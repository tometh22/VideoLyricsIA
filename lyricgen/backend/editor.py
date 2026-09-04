"""Durable editor documents, optimistic concurrency and collaboration locks."""

from __future__ import annotations

import math
import json
import uuid
import hashlib
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from database import AuditLog, EditorDocument, EditorVersion, Job, User
from segment_timing import canonicalize_editor_segments

EDITOR_REASONS = {
    "autosave", "manual", "restore", "approve", "conflict", "migration",
    "transcription", "quality_proposal",
}
EDITOR_CHECKPOINTS = EDITOR_REASONS | {"draft"}
MAX_SEGMENTS = 5000
MAX_TEXT_LENGTH = 2000
MAX_PAYLOAD_BYTES = 5 * 1024 * 1024
LOCK_SECONDS = 60
_TIMELINE_EPSILON = 1e-4


class QualityProposalsDisabled(RuntimeError):
    pass


def quality_v6_proposals_enabled() -> bool:
    return os.environ.get("QUALITY_V6_PROPOSALS_ENABLED", "0").strip().lower() in {
        "1", "true", "yes", "on",
    }


def quality_consensus_observations_enabled() -> bool:
    return os.environ.get(
        "QUALITY_CONSENSUS_OBSERVATIONS_ENABLED", "0",
    ).strip().lower() in {"1", "true", "yes", "on"}


def operator_suggestions_enabled() -> bool:
    """Return whether any human-click suggestion family is enabled."""
    return text_operator_suggestions_enabled() or timing_operator_suggestions_enabled()


def text_operator_suggestions_enabled() -> bool:
    """Text proposals are independent from automatic correction."""
    return os.environ.get(
        "QUALITY_OPERATOR_SUGGESTIONS_ENABLED", "0",
    ).strip().lower() in {"1", "true", "yes", "on"}


def timing_operator_suggestions_enabled() -> bool:
    """Timing proposals require their own explicit rollout switch."""
    return os.environ.get(
        "QUALITY_TIMING_OPERATOR_SUGGESTIONS_ENABLED", "0",
    ).strip().lower() in {"1", "true", "yes", "on"}


def operator_suggestion_type_enabled(suggestion_type: str) -> bool:
    """Route persisted proposal families through independent switches."""
    if suggestion_type == "timing":
        return timing_operator_suggestions_enabled()
    if suggestion_type in {"text", "vocalization"}:
        return text_operator_suggestions_enabled()
    return False


def revoke_quality_proposal_if_disabled(document: EditorDocument) -> bool:
    """Delete tenant-scoped proposal text when the serving switch is off."""
    proposal = document.quality_proposal
    if not isinstance(proposal, dict):
        return False
    status = str(proposal.get("status") or "pending")
    observation = proposal.get("observation_only") is True
    operator_only = proposal.get("operator_suggestion_only") is True
    should_revoke = (
        observation
        and status == "observing"
        and not quality_consensus_observations_enabled()
    ) or (
        operator_only
        and status == "pending"
        and not operator_suggestions_enabled()
    ) or (
        not observation and not operator_only
        and status == "pending"
        and not quality_v6_proposals_enabled()
    )
    if not should_revoke:
        return False
    document.quality_proposal = None
    return True


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
    provenance: dict | None = None,
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
            if existing.reason != "transcription":
                existing.reason = "approve"
        if provenance and existing.provenance is None:
            existing.provenance = provenance
        return existing
    version = EditorVersion(
        id=str(uuid.uuid4()), job_id=document.job_id, tenant_id=document.tenant_id,
        revision=revision, segments=segments, created_by=user_id,
        reason=reason, is_approved=approved, provenance=provenance,
    )
    db.add(version)
    db.flush()
    return version


def _prune_versions(db: Session, document: EditorDocument) -> None:
    # Jobs enrolled in the machine-evidence invariant are training samples.
    # Pruning their checkpoints would make the operator path impossible to
    # replay (the first real live edit hit this at revision 51).  Historical
    # jobs keep the bounded UI-history behaviour.
    if isinstance(document.machine_evidence, dict):
        return
    drafts = (
        db.query(EditorVersion)
        .filter(
            EditorVersion.job_id == document.job_id,
            EditorVersion.is_approved.is_(False),
            EditorVersion.reason != "transcription",
        )
        .order_by(EditorVersion.revision.desc())
        .all()
    )
    for stale in drafts[50:]:
        db.delete(stale)


def _record_training_delta(
    db: Session,
    *,
    job: Job,
    user_id: int,
    previous: list[dict],
    current: list[dict],
    from_revision: int,
    to_revision: int,
    checkpoint: str,
) -> bool:
    """Persist one complete material edit for evidence-enrolled jobs."""
    from correction_learning import hmac_identifier

    def protected_text_ref(value: str) -> str | None:
        try:
            return hmac_identifier("audit_lyric", value)
        except RuntimeError:
            return None

    # Historical jobs cannot satisfy the machine-snapshot invariant and are
    # ineligible for training export. Keep their cheap bounded operational
    # signal (admin funnel/activity depend on it), but never run the corpus
    # aligner or persist unbounded per-line data for those frequent autosaves.
    if not bool(getattr(job, "machine_snapshot_required", False)):
        before_by_id = {
            str(row.get("_id") if row.get("_id") not in (None, "") else f"idx_{index}"):
            (index, row)
            for index, row in enumerate(previous)
        }
        changed: list[dict] = []
        reordered: list[dict] = []
        changed_count = text_count = timing_count = reorder_count = 0
        for index, row in enumerate(current):
            row_id = str(
                row.get("_id") if row.get("_id") not in (None, "") else f"idx_{index}"
            )
            old = before_by_id.get(row_id)
            if old is None:
                continue
            old_index, old_row = old
            try:
                old_start, new_start = float(old_row.get("start") or 0), float(row.get("start") or 0)
                old_end, new_end = float(old_row.get("end") or 0), float(row.get("end") or 0)
            except (TypeError, ValueError):
                old_start = new_start = old_end = new_end = 0.0
            old_text = str(old_row.get("text") or "").strip()
            new_text = str(row.get("text") or "").strip()
            text_changed = old_text != new_text
            timing_changed = (
                abs(old_start - new_start) > 0.05
                or abs(old_end - new_end) > 0.05
            )
            if text_changed or timing_changed:
                changed_count += 1
                text_count += int(text_changed)
                timing_count += int(timing_changed)
                if len(changed) < 20:
                    changed.append({
                        "id": row_id,
                        "prev_start": round(old_start, 3),
                        "new_start": round(new_start, 3),
                        "prev_end": round(old_end, 3),
                        "new_end": round(new_end, 3),
                        "text_changed": text_changed,
                        "prev_text_length": len(old_text),
                        "new_text_length": len(new_text),
                        "prev_text_hmac": protected_text_ref(old_text),
                        "new_text_hmac": protected_text_ref(new_text),
                    })
            if old_index != index:
                reorder_count += 1
                if len(reordered) < 30:
                    reordered.append({
                        "id": row_id, "from_idx": old_index, "to_idx": index,
                    })
        if not changed_count and not reorder_count:
            return False
        db.add(AuditLog(
            user_id=user_id,
            action="lyrics.segments_diff",
            detail={
                "job_id": job.job_id,
                "n_lines": len(current),
                "changed": changed,
                "reorder": reordered,
                "correction_summary": {
                    "changed_lines": changed_count,
                    "text_changes": text_count,
                    "timing_changes": timing_count,
                    "reorders": reorder_count,
                },
                "truncated": changed_count > len(changed) or reorder_count > len(reordered),
            },
        ))
        return True

    from training_corpus import build_line_delta_audit

    detail = build_line_delta_audit(
        previous,
        current,
        job_id=job.job_id,
        from_revision=from_revision,
        to_revision=to_revision,
        checkpoint=checkpoint,
        text_ref=protected_text_ref,
    )
    if detail is None:
        return False
    db.add(AuditLog(
        user_id=user_id,
        action="lyrics.segments_diff",
        detail=detail,
    ))
    return True


def _next_revision(db: Session, document: EditorDocument, *candidates: int) -> int:
    highest_version = db.query(func.max(EditorVersion.revision)).filter(
        EditorVersion.job_id == document.job_id,
    ).scalar()
    return max(int(highest_version or 0), int(document.revision or 0), *candidates) + 1


def ensure_document(db: Session, job_id: str, tenant_id: str, segments: list[dict],
                    *, initial_reason: str = "migration",
                    initial_provenance: dict | None = None) -> EditorDocument:
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
        _ensure_version(
            db, document, revision, normalized, job.user_id, initial_reason,
            provenance=initial_provenance,
        )
    except RuntimeError:
        # Historical partial writers may have left an orphan checkpoint at
        # the Job revision. Preserve it and move the deployed Job snapshot to
        # the next free monotonic revision.
        revision = _next_revision(db, document, revision)
        document.revision = revision
        job.segments_revision = revision
        _ensure_version(
            db, document, revision, normalized, job.user_id, initial_reason,
            provenance=initial_provenance,
        )
    return document


def get_or_create_document(
    db: Session, job_id: str, tenant_id: str, segments: list[dict] | None = None,
    *, initial_reason: str = "migration",
    initial_provenance: dict | None = None,
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
        return ensure_document(
            db, job_id, tenant_id, segments, initial_reason=initial_reason,
            initial_provenance=initial_provenance,
        )

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


def attach_machine_provenance(db: Session, job_id: str, provenance: dict) -> bool:
    """Complete the initial checkpoint lineage before its transaction commits."""
    version = db.query(EditorVersion).filter(
        EditorVersion.job_id == job_id,
        EditorVersion.reason == "transcription",
    ).order_by(EditorVersion.revision.asc()).with_for_update().first()
    if version is None or version.provenance is not None:
        return False
    version.provenance = dict(provenance)
    db.flush()
    return True


def attach_machine_evidence(
    db: Session, document: EditorDocument, evidence: dict,
) -> bool:
    """Persist the private machine payload exactly once.

    A retry carrying the identical evidence is idempotent.  A different
    payload must never replace the pre-human snapshot after an editor may have
    opened it; that would corrupt the correction-learning baseline.
    """
    from machine_evidence import validate_machine_evidence

    document = db.query(EditorDocument).filter(
        EditorDocument.job_id == document.job_id,
        EditorDocument.tenant_id == document.tenant_id,
    ).populate_existing().with_for_update().one()
    validate_machine_evidence(evidence, document.original_segments)
    if document.machine_evidence is not None:
        if document.machine_evidence == evidence:
            return False
        raise RuntimeError("machine_snapshot_already_frozen")
    document.machine_evidence = dict(evidence)
    db.flush()
    return True


def require_machine_snapshot(job: Job, document: EditorDocument) -> None:
    """Fail closed for jobs enrolled in the durable-evidence contract."""
    if not bool(getattr(job, "machine_snapshot_required", False)):
        return
    from machine_evidence import validate_machine_evidence
    validate_machine_evidence(document.machine_evidence, document.original_segments)


def serialize_document(db: Session, document: EditorDocument) -> dict:
    lock_expires = _aware(document.lock_expires_at)
    lock_active = bool(lock_expires and lock_expires > now_utc())
    proposal = _proposal_for_response(document)
    return {
        "job_id": document.job_id,
        "revision": document.revision,
        "segments": document.current_segments,
        "original_segments": document.original_segments,
        "quality_proposal": proposal,
        "updated_at": _aware(document.updated_at).isoformat() if document.updated_at else None,
        "updated_by": _user_summary(db, document.updated_by),
        "lock": {
            "active": lock_active,
            "user": _user_summary(db, document.lock_user_id) if lock_active else None,
            "expires_at": lock_expires.isoformat() if lock_active else None,
        },
    }


def _proposal_for_response(document: EditorDocument) -> dict | None:
    proposal = dict(document.quality_proposal or {})
    if not proposal:
        return None
    status = str(proposal.get("status") or "pending")
    expires_at = proposal.get("expires_at")
    try:
        expires = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        expired = _aware(expires) <= now_utc()
    except (TypeError, ValueError):
        expired = True
    stale = int(proposal.get("base_revision", -1)) != int(document.revision or 0)
    if expired:
        # Raw proposed lyrics have a hard seven-day retention limit. The GET
        # editor path commits this cleanup, while the quality reconciler also
        # sweeps documents that operators never reopen.
        document.quality_proposal = None
        return None
    if status == "pending" and stale:
        return {
            "id": proposal.get("id"),
            "status": "stale",
            "base_revision": proposal.get("base_revision"),
            "expires_at": proposal.get("expires_at"),
            "windows": [],
        }
    return proposal


def expire_stale_quality_proposals(
    db: Session, *, now=None, limit: int | None = None,
) -> int:
    """Erase expired payloads under row lock, rechecking after lock acquisition."""
    now = _aware(now) or now_utc()
    expired = 0
    candidates = db.query(EditorDocument.job_id).filter(
        EditorDocument.quality_proposal.isnot(None),
    ).order_by(EditorDocument.updated_at.asc(), EditorDocument.job_id.asc())
    if limit is not None:
        candidates = candidates.limit(max(1, min(int(limit), 1000)))
    candidate_ids = [row[0] for row in candidates.all()]
    for job_id in candidate_ids:
        query = db.query(EditorDocument).filter(EditorDocument.job_id == job_id)
        if db.bind is not None and db.bind.dialect.name == "postgresql":
            query = query.with_for_update(skip_locked=True)
        else:
            query = query.with_for_update()
        document = query.populate_existing().first()
        if document is None or document.quality_proposal is None:
            continue
        proposal = document.quality_proposal
        if not isinstance(proposal, dict):
            document.quality_proposal = None
            expired += 1
            continue
        try:
            expires = _aware(datetime.fromisoformat(
                str(proposal.get("expires_at") or "").replace("Z", "+00:00")
            ))
        except (TypeError, ValueError):
            expires = None
        if expires is None or expires <= now:
            document.quality_proposal = None
            expired += 1
    if expired:
        db.flush()
    return expired


def proposal_idempotency_hash(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def segments_content_hash(value: list[dict]) -> str:
    """Hash every editor field and list order, unlike ASR diagnostic hashes."""
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _strict_timeline(value: Any, *, label: str) -> list[dict]:
    """Validate without canonical repair so malformed proposals fail closed."""
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    rows: list[dict] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"{label} segment {index} must be an object")
        try:
            start, end = float(item.get("start")), float(item.get("end"))
        except (TypeError, ValueError):
            raise ValueError(f"{label} segment {index} has invalid timing") from None
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
            raise ValueError(f"{label} segment {index} has invalid timing")
        rows.append({**item, "start": round(start, 4), "end": round(end, 4)})
    original_order = [
        (row["start"], row["end"], str(row.get("text") or "")) for row in rows
    ]
    rows.sort(key=lambda row: (row["start"], row["end"], str(row.get("text") or "")))
    sorted_order = [
        (row["start"], row["end"], str(row.get("text") or "")) for row in rows
    ]
    if original_order != sorted_order:
        raise ValueError(f"{label} is not monotonic")
    seen = set()
    previous = None
    for index, row in enumerate(rows):
        digest = segments_content_hash([row])
        if digest in seen:
            raise ValueError(f"{label} contains a duplicate segment")
        seen.add(digest)
        if previous is not None:
            if abs(row["start"] - previous["start"]) <= _TIMELINE_EPSILON:
                raise ValueError(f"{label} contains duplicate starts")
            if row["start"] < previous["end"] - _TIMELINE_EPSILON:
                raise ValueError(f"{label} contains overlapping segments")
        previous = row
    return rows


def _segments_overlapping_window(
    segments: list[dict], start: float, end: float,
) -> list[dict]:
    return [
        dict(item) for item in segments
        if float(item["start"]) < end - _TIMELINE_EPSILON
        and float(item["end"]) > start + _TIMELINE_EPSILON
    ]


def _validate_review_proposal_against_document(
    proposal: dict, current_segments: list[dict], *, require_hashes: bool = False,
) -> dict:
    current = _strict_timeline(current_segments, label="current timeline")
    windows = proposal.get("windows")
    if not isinstance(windows, list) or not windows:
        raise ValueError("quality proposal requires windows")
    validated: list[dict] = []
    claimed_current_hashes: set[str] = set()
    for index, window in enumerate(windows):
        if not isinstance(window, dict):
            raise ValueError(f"quality proposal window {index} is invalid")
        try:
            start, end = float(window.get("start")), float(window.get("end"))
        except (TypeError, ValueError):
            raise ValueError("quality proposal window timing is invalid") from None
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
            raise ValueError("quality proposal window timing is invalid")
        supplied_current = _strict_timeline(
            window.get("current_segments"), label=f"window {index} current",
        )
        expected_current = _segments_overlapping_window(current, start, end)
        if supplied_current != expected_current:
            raise ValueError("quality proposal current segments do not match editor snapshot")
        if any(
            row["start"] < start - _TIMELINE_EPSILON
            or row["end"] > end + _TIMELINE_EPSILON
            for row in supplied_current
        ):
            raise ValueError("quality proposal window cuts through a current segment")
        proposed = _strict_timeline(
            window.get("proposed_segments"), label=f"window {index} proposed",
        )
        if not proposed:
            raise ValueError("quality proposal window requires proposed segments")
        if any(
            row["start"] < start - _TIMELINE_EPSILON
            or row["end"] > end + _TIMELINE_EPSILON
            for row in proposed
        ):
            raise ValueError("quality proposal segment falls outside its window")
        current_hash = segments_content_hash(supplied_current)
        proposed_hash = segments_content_hash(proposed)
        if require_hashes and (
            str(window.get("current_segments_hash") or "") != current_hash
            or str(window.get("proposed_segments_hash") or "") != proposed_hash
        ):
            raise ValueError("quality proposal window hash mismatch")
        individual_hashes = {segments_content_hash([row]) for row in supplied_current}
        if claimed_current_hashes.intersection(individual_hashes):
            raise ValueError("quality proposal windows duplicate current segments")
        claimed_current_hashes.update(individual_hashes)
        validated.append({
            **window,
            "start": round(start, 4), "end": round(end, 4),
            "current_segments": supplied_current,
            "proposed_segments": proposed,
            "current_segments_hash": current_hash,
            "proposed_segments_hash": proposed_hash,
        })
    validated.sort(key=lambda item: (item["start"], item["end"], str(item.get("id"))))
    for left, right in zip(validated, validated[1:]):
        if right["start"] < left["end"] - _TIMELINE_EPSILON:
            raise ValueError("quality proposal windows overlap")
    return {**proposal, "windows": validated}


def persist_quality_proposal_if_current(
    db: Session,
    *,
    job_id: str,
    expected_revision: int,
    expected_segments_hash: str,
    expected_audio_revision: int,
    expected_audio_sha256: str,
    proposal: dict,
) -> bool:
    """Store raw suggestions only on the exact tenant-scoped editor snapshot."""
    from quality_v6_contracts import ReviewProposal
    from transcription_quality import segments_hash

    job = db.query(Job).filter(Job.job_id == job_id).with_for_update().first()
    document = db.query(EditorDocument).filter(
        EditorDocument.job_id == job_id,
    ).with_for_update().first()
    if job is None or document is None:
        return False
    if not quality_v6_proposals_enabled():
        revoke_quality_proposal_if_disabled(document)
        db.flush()
        return False
    try:
        proposal = ReviewProposal.from_mapping(proposal).to_dict()
    except (TypeError, ValueError):
        return False
    if (
        int(document.revision or 0) != int(expected_revision)
        or segments_hash(document.current_segments or []) != expected_segments_hash
        or int(job.audio_revision or 0) != int(expected_audio_revision)
        or str(job.input_audio_sha256 or "") != str(expected_audio_sha256 or "")
    ):
        return False
    try:
        proposal = _validate_review_proposal_against_document(
            proposal, list(document.current_segments or []),
        )
    except ValueError:
        return False
    created = now_utc()
    document.quality_proposal = {
        **proposal,
        "id": str(proposal.get("id") or uuid.uuid4()),
        "status": "pending",
        "base_revision": int(expected_revision),
        "segments_hash": expected_segments_hash,
        "segments_content_hash": segments_content_hash(
            list(document.current_segments or []),
        ),
        "audio_revision": int(expected_audio_revision),
        "audio_sha256": expected_audio_sha256,
        "created_at": created.isoformat(),
        "expires_at": (created + timedelta(days=7)).isoformat(),
    }
    db.flush()
    return True


def persist_quality_observation_if_current(
    db: Session,
    *,
    job_id: str,
    expected_revision: int,
    expected_segments_hash: str,
    expected_audio_revision: int,
    expected_audio_sha256: str,
    proposal: dict,
) -> bool:
    """Store a non-applicable suggestion for human calibration.

    This path intentionally does not call the production proposal
    authorization.  Its stored status is ``observing`` and every mutation
    endpoint rejects it, so it can collect labels without bypassing the
    signed Quality v6 certificate.
    """
    from quality_v6_contracts import ReviewProposal
    from transcription_quality import segments_hash

    if not quality_consensus_observations_enabled():
        return False
    if proposal.get("observation_only") is not True:
        return False
    raw_windows = {
        str(item.get("id")): dict(item)
        for item in (proposal.get("windows") or []) if isinstance(item, dict)
    }
    try:
        typed = ReviewProposal.from_mapping(proposal).to_dict()
    except (TypeError, ValueError):
        return False
    for window in typed["windows"]:
        raw = raw_windows.get(str(window.get("id"))) or {}
        families = raw.get("source_families")
        if not isinstance(families, list) or len(set(families)) < 2:
            return False
        window["source_families"] = sorted(set(str(item) for item in families))

    job = db.query(Job).filter(Job.job_id == job_id).with_for_update().first()
    document = db.query(EditorDocument).filter(
        EditorDocument.job_id == job_id,
    ).with_for_update().first()
    if job is None or document is None:
        return False
    if (
        int(document.revision or 0) != int(expected_revision)
        or segments_hash(document.current_segments or []) != expected_segments_hash
        or int(job.audio_revision or 0) != int(expected_audio_revision)
        or str(job.input_audio_sha256 or "") != str(expected_audio_sha256 or "")
    ):
        return False
    try:
        typed = _validate_review_proposal_against_document(
            typed, list(document.current_segments or []),
        )
    except ValueError:
        return False
    created = now_utc()
    document.quality_proposal = {
        **typed,
        "id": str(proposal.get("id") or uuid.uuid4()),
        "status": "observing",
        "observation_only": True,
        "certificate_policy_version": "independent-consensus-review-policy-v1",
        "base_revision": int(expected_revision),
        "segments_hash": expected_segments_hash,
        "segments_content_hash": segments_content_hash(
            list(document.current_segments or []),
        ),
        "audio_revision": int(expected_audio_revision),
        "audio_sha256": expected_audio_sha256,
        "created_at": created.isoformat(),
        "expires_at": (created + timedelta(days=7)).isoformat(),
    }
    db.flush()
    return True


def persist_operator_review_proposal_if_current(
    db: Session,
    *,
    job_id: str,
    expected_revision: int,
    expected_segments_hash: str,
    expected_audio_revision: int,
    expected_audio_sha256: str,
    proposal: dict,
) -> bool:
    """Persist auditable one-click suggestions without authorizing automation."""
    from transcription_quality import segments_hash

    if not operator_suggestions_enabled():
        return False
    if not (
        isinstance(proposal, dict)
        and proposal.get("kind") == "operator_review_proposal"
        and proposal.get("schema") == "operator-review-proposal-v1"
        and proposal.get("review_only") is True
        and proposal.get("operator_suggestion_only") is True
        and proposal.get("automatic_apply_allowed") is False
    ):
        return False
    job = db.query(Job).filter(Job.job_id == job_id).with_for_update().first()
    document = db.query(EditorDocument).filter(
        EditorDocument.job_id == job_id,
    ).with_for_update().first()
    if job is None or document is None:
        return False
    if (
        int(document.revision or 0) != int(expected_revision)
        or segments_hash(document.current_segments or []) != expected_segments_hash
        or int(job.audio_revision or 0) != int(expected_audio_revision)
        or str(job.input_audio_sha256 or "") != str(expected_audio_sha256 or "")
    ):
        return False

    # Once an operator has changed a line, the original machine snapshot no
    # longer contains that exact row. Suggestions on that line fail closed.
    original_keys = {
        _segment_key(dict(item))
        for item in (document.original_segments or []) if isinstance(item, dict)
    }
    eligible_windows = []
    for window in proposal.get("windows") or []:
        if not isinstance(window, dict):
            continue
        current = [
            dict(item) for item in (window.get("current_segments") or [])
            if isinstance(item, dict)
        ]
        suggestion_type = str(window.get("suggestion_type") or "")
        if not operator_suggestion_type_enabled(suggestion_type):
            continue
        if (not current and suggestion_type == "timing") or any(
            item.get("locked") is True or item.get("operator_locked") is True
            or _segment_key(item) not in original_keys
            for item in current
        ):
            continue
        if (
            suggestion_type not in {"timing", "text", "vocalization"}
            or str(window.get("confidence") or "")
            not in {"high", "medium", "low"}
            or window.get("automatic_apply_allowed") is not False
        ):
            continue
        eligible_windows.append(dict(window))
    if not eligible_windows:
        return False
    try:
        validated = _validate_review_proposal_against_document(
            {**proposal, "windows": eligible_windows},
            list(document.current_segments or []),
        )
    except ValueError:
        return False
    created = now_utc()
    document.quality_proposal = {
        **validated,
        "id": str(proposal.get("id") or uuid.uuid4()),
        "status": "pending",
        "base_revision": int(expected_revision),
        "segments_hash": expected_segments_hash,
        "segments_content_hash": segments_content_hash(
            list(document.current_segments or []),
        ),
        "audio_revision": int(expected_audio_revision),
        "audio_sha256": expected_audio_sha256,
        "created_at": created.isoformat(),
        "expires_at": (created + timedelta(days=7)).isoformat(),
    }
    db.flush()
    return True


def record_quality_observation(
    db: Session,
    job: Job,
    document: EditorDocument,
    *,
    proposal_id: str,
    window_id: str,
    base_revision: int,
    verdict: str,
    idempotency_key: str,
) -> tuple[dict, bool]:
    """Record one hash-only human verdict without changing editor content."""
    if verdict not in {"correct", "incorrect", "uncertain"}:
        raise ValueError("invalid_quality_observation_verdict")
    job = db.query(Job).filter(Job.job_id == job.job_id).with_for_update().one()
    document = db.query(EditorDocument).filter(
        EditorDocument.job_id == document.job_id,
    ).populate_existing().with_for_update().one()
    proposal = dict(document.quality_proposal or {})
    if (
        proposal.get("id") != proposal_id
        or proposal.get("observation_only") is not True
    ):
        raise LookupError("quality_observation_not_found")
    if int(document.revision or 0) != int(base_revision):
        raise RuntimeError("editor_revision_conflict")
    if (
        int(proposal.get("audio_revision", -1)) != int(job.audio_revision or 0)
        or str(proposal.get("audio_sha256") or "") != str(job.input_audio_sha256 or "")
        or str(proposal.get("segments_content_hash") or "")
        != segments_content_hash(list(document.current_segments or []))
    ):
        raise RuntimeError("quality_observation_stale")
    windows = [dict(item) for item in (proposal.get("windows") or [])]
    matching = [item for item in windows if str(item.get("id")) == str(window_id)]
    if len(matching) != 1:
        raise LookupError("quality_observation_window_not_found")
    window = matching[0]
    idem_hash = proposal_idempotency_hash(idempotency_key)
    existing = window.get("human_verdict")
    if existing:
        if (
            existing == verdict
            and window.get("verdict_idempotency_hash") == idem_hash
        ):
            return dict(window.get("observation_evidence") or {}), False
        raise RuntimeError("quality_observation_already_recorded")

    from consensus_review_certificate import (
        OBSERVATION_SCHEMA, canonical_source_family,
    )
    from evidence_contracts import privacy_fingerprint
    families = sorted({
        canonical_source_family(item)
        for item in (window.get("source_families") or [])
        if canonical_source_family(item)
    })
    if len(families) < 2:
        raise RuntimeError("independent_source_family_missing")

    def private_digest(namespace: str, value: Any) -> str:
        fingerprint = privacy_fingerprint(namespace, value)
        if not fingerprint:
            raise RuntimeError("quality_observation_privacy_key_missing")
        return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()

    evidence = {
        "schema": OBSERVATION_SCHEMA,
        "window_id": private_digest(
            "consensus-observation-window",
            {"job_id": job.job_id, "proposal_id": proposal_id, "window_id": window_id},
        ),
        "song_id": private_digest("consensus-observation-song", job.job_id),
        "verdict": verdict,
        "source_families": families,
        "candidate_sha256": private_digest(
            "consensus-observation-candidate", window.get("proposed_segments") or [],
        ),
        "audio_window_sha256": private_digest(
            "consensus-observation-audio-window", {
                "audio_sha256": job.input_audio_sha256,
                "start": window.get("start"), "end": window.get("end"),
            },
        ),
    }
    moment = now_utc().isoformat()
    for index, item in enumerate(windows):
        if str(item.get("id")) == str(window_id):
            windows[index] = {
                **item,
                "human_verdict": verdict,
                "reviewed_at": moment,
                "verdict_idempotency_hash": idem_hash,
                "observation_evidence": evidence,
            }
            break
    complete = all(item.get("human_verdict") for item in windows)
    if complete:
        # The certificate consumes only hashes. Erase raw current/proposed
        # lyrics as soon as every window is judged while retaining enough
        # state for an idempotent retry of a response lost in transit.
        windows = [{
            "id": item.get("id"),
            "start": item.get("start"), "end": item.get("end"),
            "human_verdict": item.get("human_verdict"),
            "reviewed_at": item.get("reviewed_at"),
            "verdict_idempotency_hash": item.get("verdict_idempotency_hash"),
            "observation_evidence": item.get("observation_evidence"),
        } for item in windows]
    proposal["windows"] = windows
    proposal["status"] = "observed" if complete else "observing"
    document.quality_proposal = proposal
    db.flush()
    return evidence, True


def _segment_key(segment: dict) -> tuple:
    row_id = segment.get("_id") or segment.get("id") or segment.get("segment_id")
    if row_id:
        return ("id", str(row_id))
    return (
        "value", round(float(segment.get("start") or 0), 4),
        round(float(segment.get("end") or 0), 4), str(segment.get("text") or ""),
    )


def apply_quality_proposal(
    db: Session,
    job: Job,
    document: EditorDocument,
    user_id: int,
    *,
    proposal_id: str,
    base_revision: int,
    window_ids: list[str],
    idempotency_key: str,
) -> tuple[EditorDocument, EditorVersion | None, bool]:
    from transcription_quality import segments_hash

    job = db.query(Job).filter(
        Job.job_id == job.job_id,
    ).populate_existing().with_for_update().one()
    document = db.query(EditorDocument).filter(
        EditorDocument.job_id == document.job_id,
    ).populate_existing().with_for_update().one()
    proposal = dict(document.quality_proposal or {})
    operator_only = proposal.get("operator_suggestion_only") is True
    if operator_only:
        if not operator_suggestions_enabled():
            revoke_quality_proposal_if_disabled(document)
            db.flush()
            raise QualityProposalsDisabled("operator_suggestions_disabled")
    elif not quality_v6_proposals_enabled():
        revoke_quality_proposal_if_disabled(document)
        db.flush()
        raise QualityProposalsDisabled("quality_v6_proposals_disabled")
    if proposal.get("observation_only") is True:
        raise QualityProposalsDisabled("quality_observation_cannot_be_applied")
    idem_hash = proposal_idempotency_hash(idempotency_key)
    if proposal.get("id") != proposal_id:
        raise LookupError("quality_proposal_not_found")
    if any(
        isinstance(item, dict)
        and item.get("idempotency_hash") == idem_hash
        and item.get("decision") == "accepted"
        for item in (proposal.get("decision_history") or [])
    ):
        return document, None, False
    if proposal.get("status") == "applied" and proposal.get("idempotency_hash") == idem_hash:
        return document, None, False
    visible_proposal = _proposal_for_response(document) or {}
    if proposal.get("status") != "pending" or visible_proposal.get("status") != "pending":
        raise RuntimeError("quality_proposal_stale")
    if (
        int(proposal.get("audio_revision", -1)) != int(job.audio_revision or 0)
        or str(proposal.get("audio_sha256") or "")
        != str(job.input_audio_sha256 or "")
        or str(proposal.get("segments_hash") or "")
        != segments_hash(document.current_segments or [])
        or str(proposal.get("segments_content_hash") or "")
        != segments_content_hash(list(document.current_segments or []))
    ):
        raise RuntimeError("quality_proposal_stale")
    if int(document.revision or 0) != int(base_revision):
        raise RuntimeError("editor_revision_conflict")
    requested = [str(item) for item in window_ids]
    selected = set(requested)
    if len(requested) != len(selected):
        raise ValueError("quality proposal window ids must be unique")
    windows = [
        item for item in (proposal.get("windows") or [])
        if isinstance(item, dict) and str(item.get("id")) in selected
    ]
    if not windows or len(windows) != len(selected):
        raise ValueError("invalid quality proposal windows")
    validated_proposal = _validate_review_proposal_against_document(
        proposal, list(document.current_segments or []), require_hashes=True,
    )
    validated_by_id = {
        str(item.get("id")): item for item in validated_proposal["windows"]
    }
    windows = [validated_by_id[item] for item in selected]
    segments = [dict(item) for item in (document.current_segments or [])]
    for window in windows:
        remove_hashes = {
            segments_content_hash([dict(item)])
            for item in (window.get("current_segments") or [])
        }
        segments = [
            item for item in segments
            if segments_content_hash([dict(item)]) not in remove_hashes
        ]
        segments.extend(
            dict(item) for item in (window.get("proposed_segments") or [])
            if isinstance(item, dict)
        )
    # Replacement rows are appended per selected window. Re-establish the
    # canonical global order before the strict overlap/duplicate validation.
    segments.sort(key=lambda row: (
        float(row.get("start") or 0), float(row.get("end") or 0),
        str(row.get("text") or ""),
    ))
    segments = _strict_timeline(segments, label="quality proposal result")
    document, version, applied = save_document(
        db, job, document, user_id, base_revision, segments, "quality_proposal",
    )
    history = [
        dict(item) for item in (proposal.get("decision_history") or [])
        if isinstance(item, dict)
    ][-99:]
    history.append({
        "decision": "accepted",
        "window_ids": sorted(selected),
        "idempotency_hash": idem_hash,
        "decided_at": now_utc().isoformat(),
    })
    remaining = [
        dict(item) for item in validated_proposal["windows"]
        if str(item.get("id")) not in selected
    ]
    if operator_only and remaining:
        current = list(document.current_segments or [])
        document.quality_proposal = {
            **proposal,
            "status": "pending",
            "windows": remaining,
            "base_revision": int(document.revision or 0),
            "segments_hash": segments_hash(current),
            "segments_content_hash": segments_content_hash(current),
            "decision_history": history,
            "expires_at": (now_utc() + timedelta(days=7)).isoformat(),
        }
    else:
        document.quality_proposal = {
            "id": proposal_id, "status": "applied", "windows": [],
            "base_revision": base_revision,
            "applied_revision": int(document.revision or 0),
            "idempotency_hash": idem_hash,
            "decision_history": history,
            "expires_at": (now_utc() + timedelta(days=7)).isoformat(),
        }
    db.flush()
    return document, version, applied


def reject_operator_suggestion(
    db: Session,
    document: EditorDocument,
    *,
    proposal_id: str,
    window_id: str,
    base_revision: int,
    reason: str,
    idempotency_key: str,
) -> tuple[dict, bool]:
    """Reject one human-only suggestion and retain the rest of the batch."""

    allowed_reasons = {
        "operator_rejected", "incorrect_content", "incorrect_timing",
        "not_helpful", "already_fixed", "uncertain",
    }
    if reason not in allowed_reasons:
        raise ValueError("invalid_operator_suggestion_reason")
    document = db.query(EditorDocument).filter(
        EditorDocument.job_id == document.job_id,
    ).populate_existing().with_for_update().one()
    proposal = dict(document.quality_proposal or {})
    if (
        proposal.get("id") != proposal_id
        or proposal.get("operator_suggestion_only") is not True
    ):
        raise LookupError("operator_suggestion_not_found")
    if not operator_suggestions_enabled():
        revoke_quality_proposal_if_disabled(document)
        db.flush()
        raise QualityProposalsDisabled("operator_suggestions_disabled")
    idem_hash = proposal_idempotency_hash(idempotency_key)
    history = [
        dict(item) for item in (proposal.get("decision_history") or [])
        if isinstance(item, dict)
    ][-99:]
    for item in history:
        if (
            item.get("idempotency_hash") == idem_hash
            and item.get("decision") == "rejected"
        ):
            return dict(item), False
    if proposal.get("status") != "pending":
        raise RuntimeError("quality_proposal_stale")
    if int(document.revision or 0) != int(base_revision):
        raise RuntimeError("editor_revision_conflict")
    windows = [
        dict(item) for item in (proposal.get("windows") or [])
        if isinstance(item, dict)
    ]
    target = next(
        (item for item in windows if str(item.get("id")) == str(window_id)),
        None,
    )
    if target is None:
        raise LookupError("operator_suggestion_not_found")
    evidence = {
        "decision": "rejected",
        "window_id": hashlib.sha256(str(window_id).encode()).hexdigest()[:16],
        "suggestion_type": str(target.get("suggestion_type") or "unknown"),
        "confidence": str(target.get("confidence") or "unknown"),
        "impact_ms": int(target.get("impact_ms") or 0),
        "proposed_delta_ms": round(1000 * (
            float(target.get("proposed_end") or 0)
            - float(target.get("current_end") or 0)
        )) if target.get("suggestion_type") == "timing" else None,
        "reason": reason,
        "idempotency_hash": idem_hash,
        "decided_at": now_utc().isoformat(),
    }
    history.append(evidence)
    remaining = [
        item for item in windows if str(item.get("id")) != str(window_id)
    ]
    document.quality_proposal = {
        **proposal,
        "status": "pending" if remaining else "dismissed",
        "windows": remaining,
        "decision_history": history,
        "expires_at": (now_utc() + timedelta(days=7)).isoformat(),
    }
    db.flush()
    return evidence, True


def dismiss_quality_proposal(
    db: Session, document: EditorDocument, *, proposal_id: str,
    base_revision: int, idempotency_key: str,
) -> bool:
    document = db.query(EditorDocument).filter(
        EditorDocument.job_id == document.job_id,
    ).populate_existing().with_for_update().one()
    proposal = dict(document.quality_proposal or {})
    idem_hash = proposal_idempotency_hash(idempotency_key)
    if proposal.get("id") != proposal_id:
        raise LookupError("quality_proposal_not_found")
    if proposal.get("status") == "dismissed" and proposal.get("idempotency_hash") == idem_hash:
        return False
    if int(document.revision or 0) != int(base_revision):
        raise RuntimeError("editor_revision_conflict")
    document.quality_proposal = {
        "id": proposal_id, "status": "dismissed", "windows": [],
        "base_revision": base_revision, "idempotency_hash": idem_hash,
        "expires_at": (now_utc() + timedelta(days=7)).isoformat(),
    }
    db.flush()
    return True


def rebase_operator_suggestions_after_manual_edit(
    document: EditorDocument,
    proposal: dict | None,
    previous_segments: list[dict],
) -> list[dict]:
    """Keep untouched suggestions and record only rows resolved by hand.

    Ordinary autosave invalidates model proposals. Operator-only suggestions
    are a review worklist: editing one line must not erase unrelated work, and
    the chosen timing delta is calibration evidence. Returned evidence is
    deliberately text-free and safe for ProductEvent.
    """
    proposal = dict(proposal or {})
    from transcription_quality import segments_hash
    if (
        proposal.get("operator_suggestion_only") is not True
        or proposal.get("status") != "pending"
    ):
        return []

    before = [dict(item) for item in (previous_segments or [])
              if isinstance(item, dict)]
    after = [dict(item) for item in (document.current_segments or [])
             if isinstance(item, dict)]

    def semantic_key(row: dict) -> tuple:
        return (
            round(float(row.get("start") or 0), 4),
            round(float(row.get("end") or 0), 4),
            str(row.get("text") or ""),
        )

    after_keys = {semantic_key(item) for item in after}
    history = [
        dict(item) for item in (proposal.get("decision_history") or [])
        if isinstance(item, dict)
    ][-99:]
    remaining: list[dict] = []
    decisions: list[dict] = []

    def original_index(row: dict) -> int | None:
        key = _segment_key(row)
        for index, item in enumerate(before):
            if _segment_key(item) == key:
                return index
        return None

    def chosen_row(row: dict, index: int | None) -> dict | None:
        # Stable editor ordering is the strongest occurrence identity for
        # repeated choruses. Fall back only within the same local start/text.
        row_id = row.get("_id") or row.get("id") or row.get("segment_id")
        if row_id:
            identified = next(
                (item for item in after if _segment_key(item) == _segment_key(row)),
                None,
            )
            if identified is not None:
                return identified
        if index is not None and index < len(after):
            candidate = after[index]
            if (
                str(candidate.get("text") or "").strip().casefold()
                == str(row.get("text") or "").strip().casefold()
                or abs(float(candidate.get("start") or 0)
                       - float(row.get("start") or 0)) <= 0.75
            ):
                return candidate
        candidates = [
            item for item in after
            if str(item.get("text") or "").strip().casefold()
            == str(row.get("text") or "").strip().casefold()
            and abs(float(item.get("start") or 0)
                    - float(row.get("start") or 0)) <= 0.75
        ]
        return min(
            candidates,
            key=lambda item: abs(float(item.get("start") or 0)
                                 - float(row.get("start") or 0)),
            default=None,
        )

    for raw in proposal.get("windows") or []:
        if not isinstance(raw, dict):
            continue
        window = dict(raw)
        current = [dict(item) for item in (window.get("current_segments") or [])
                   if isinstance(item, dict)]
        final_row = None
        if current:
            target = current[0]
            if semantic_key(target) in after_keys:
                remaining.append(window)
                continue
            final_row = chosen_row(target, original_index(target))
        else:
            # Gap suggestion: resolve it manually only when this edit added
            # content into that exact acoustic window.
            start = float(window.get("start") or 0)
            end = float(window.get("end") or start)
            prior_overlap = any(
                min(end, float(item.get("end") or 0))
                - max(start, float(item.get("start") or 0)) > 0.20
                for item in before
            )
            new_overlap = [
                item for item in after
                if min(end, float(item.get("end") or 0))
                - max(start, float(item.get("start") or 0)) > 0.20
            ]
            if not prior_overlap and new_overlap:
                final_row = new_overlap[0]
            else:
                remaining.append(window)
                continue

        suggestion_type = str(window.get("suggestion_type") or "unknown")
        current_end = window.get("current_end")
        proposed_end = window.get("proposed_end")
        chosen_end = final_row.get("end") if isinstance(final_row, dict) else None
        proposed_delta_ms = chosen_delta_ms = distance_ms = None
        if suggestion_type == "timing":
            try:
                proposed_delta_ms = round(1000 * (
                    float(proposed_end) - float(current_end)
                ))
                if chosen_end is not None:
                    chosen_delta_ms = round(1000 * (
                        float(chosen_end) - float(current_end)
                    ))
                    distance_ms = round(1000 * (
                        float(chosen_end) - float(proposed_end)
                    ))
            except (TypeError, ValueError):
                proposed_delta_ms = chosen_delta_ms = distance_ms = None
        evidence = {
            "decision": "manual_override",
            "window_id": hashlib.sha256(
                str(window.get("id") or "").encode()
            ).hexdigest()[:16],
            "suggestion_type": suggestion_type,
            "confidence": str(window.get("confidence") or "unknown"),
            "impact_ms": int(window.get("impact_ms") or 0),
            "proposed_delta_ms": proposed_delta_ms,
            "chosen_delta_ms": chosen_delta_ms,
            "distance_to_proposal_ms": distance_ms,
            "decided_at": now_utc().isoformat(),
        }
        decisions.append(evidence)
        history.append(evidence)

    if remaining:
        document.quality_proposal = {
            **proposal,
            "status": "pending",
            "windows": remaining,
            "base_revision": int(document.revision or 0),
            "segments_hash": segments_hash(after),
            "segments_content_hash": segments_content_hash(after),
            "decision_history": history[-100:],
            "expires_at": (now_utc() + timedelta(days=7)).isoformat(),
        }
    else:
        document.quality_proposal = {
            "id": proposal.get("id"),
            "status": "manually_resolved" if decisions else "stale",
            "windows": [],
            "base_revision": int(document.revision or 0),
            "decision_history": history[-100:],
            "expires_at": (now_utc() + timedelta(days=7)).isoformat(),
        }
    return decisions


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
    previous_segments = [dict(item) for item in (document.current_segments or [])]
    previous_revision = int(document.revision or 0)
    document.current_segments = normalized
    document.revision += 1
    document.updated_by = user_id
    document.updated_at = now_utc()
    job.segments_json = normalized
    job.segments_revision = document.revision
    # Any ordinary edit makes a raw proposal stale. Quality-proposal apply
    # writes a text-free tombstone after save_document returns.
    document.quality_proposal = None
    _record_training_delta(
        db,
        job=job,
        user_id=user_id,
        previous=previous_segments,
        current=normalized,
        from_revision=previous_revision,
        to_revision=int(document.revision),
        checkpoint=reason,
    )
    version = None
    # For evidence-enrolled jobs even the fast 800 ms draft is part of the
    # recoverable operator path.  Store it as an autosave checkpoint so the
    # raw tenant-private text is replayable without leaking it into AuditLog.
    persist_training_draft = bool(getattr(job, "machine_snapshot_required", False))
    if reason != "draft" or persist_training_draft:
        version = _ensure_version(
            db, document, document.revision, normalized, user_id,
            "autosave" if reason == "draft" else reason,
            approved=reason == "approve",
        )
        _prune_versions(db, document)
    db.flush()
    return document, version, True


def acquire_lock(
    db: Session,
    document: EditorDocument,
    user_id: int,
    *,
    session_id: str | None = None,
) -> dict:
    document = db.query(EditorDocument).filter(
        EditorDocument.job_id == document.job_id,
        EditorDocument.tenant_id == document.tenant_id,
    ).populate_existing().with_for_update().one()
    now = now_utc()
    expires = _aware(document.lock_expires_at)
    owned_by_other_user = document.lock_user_id not in (None, user_id)
    owned_by_other_tab = bool(
        session_id
        and document.lock_session_id
        and document.lock_session_id != session_id
    )
    if expires and expires > now and (owned_by_other_user or owned_by_other_tab):
        return {
            "acquired": False,
            "user": _user_summary(db, document.lock_user_id),
            "expires_at": expires.isoformat(),
            "other_session": owned_by_other_tab,
        }
    document.lock_user_id = user_id
    document.lock_session_id = session_id or document.lock_session_id
    document.lock_expires_at = now + timedelta(seconds=LOCK_SECONDS)
    db.flush()
    return {
        "acquired": True,
        "user": _user_summary(db, user_id),
        "expires_at": document.lock_expires_at.isoformat(),
        "session_id": session_id,
    }


def release_lock(
    db: Session,
    document: EditorDocument,
    user_id: int,
    *,
    session_id: str | None = None,
) -> bool:
    document = db.query(EditorDocument).filter(
        EditorDocument.job_id == document.job_id,
        EditorDocument.tenant_id == document.tenant_id,
    ).populate_existing().with_for_update().one()
    if document.lock_user_id not in (None, user_id):
        return False
    if session_id and document.lock_session_id and document.lock_session_id != session_id:
        return False
    document.lock_user_id = None
    document.lock_session_id = None
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
    job = db.query(Job).filter(Job.job_id == document.job_id).with_for_update().one()
    previous_segments = [dict(item) for item in (document.current_segments or [])]
    previous_revision = int(document.revision or 0)
    document.current_segments = normalized
    document.revision = revision
    document.updated_by = user_id
    document.updated_at = now_utc()
    _ensure_version(db, document, revision, normalized, user_id, "autosave")
    _record_training_delta(
        db,
        job=job,
        user_id=user_id,
        previous=previous_segments,
        current=normalized,
        from_revision=previous_revision,
        to_revision=revision,
        checkpoint="legacy_autosave",
    )
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
    require_machine_snapshot(job, document)
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
    if version.reason != "transcription":
        version.reason = "approve"
    freeze_approval_training_evidence(job, version)
    job.segments_json = normalize_segments(document.current_segments)
    job.segments_revision = document.revision
    db.flush()
    return document, version


def freeze_approval_training_evidence(job: Job, version: EditorVersion) -> None:
    """Attach the quality traffic light to the exact approved lyric version."""
    from machine_evidence import approval_training_provenance, snapshot_hash

    existing = dict(version.provenance or {})
    frozen = dict(existing.get("training_approval") or {})
    if (
        frozen.get("schema") == "training-approval-evidence-v1"
        and frozen.get("segments_sha256") == snapshot_hash(list(version.segments or []))
    ):
        return
    version.provenance = {
        **existing,
        "training_approval": approval_training_provenance(
            segments=list(version.segments or []),
            quality=getattr(job, "transcription_quality", None),
            revision=int(version.revision),
        ),
    }
