"""Reviewer-requested re-transcription with a forced language.

Real case (staging job 7d45ea03941b, "Gran Ídolo De Asia"): WhisperX
auto-detected English and translated half of a Spanish song.  The job already
had saved revisions, so neither ``/transcribe-uploaded`` (409 on revision > 0)
nor the campaign retry (auto language, and the worker discards results over
human revisions) could fix it.

This module is the non-destructive product path:

1. ``start`` (API, under the Job row lock) checkpoints the current human text
   as an ordinary ``editor_versions`` row, copies the frozen machine snapshot
   (``original_segments`` + ``machine_evidence``) and the quality blob into an
   append-only ``editor_retranscriptions`` row, and enqueues a fresh
   transcription of the same stored audio through the regular outbox with the
   language forced.
2. ``install_baseline`` (worker, under the Job row lock, only for the attempt
   that the archive row names) installs the new machine output as a NEW
   revision and as the new pre-human baseline.  Nothing is deleted: previous
   versions and correction observations stay exactly as they were.
3. ``settle_failed`` returns a failed attempt to the editable state with the
   previous text intact.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from transcription_language import SUPPORTED_LANGUAGES

RETRANSCRIPTION_REASON = "retranscription"
PRE_RETRANSCRIPTION_REASON = "pre_retranscription"
AUDIT_ACTION = "lyrics.language_retranscription_requested"

# A transcription (or its separation stage) is in flight.
ACTIVE_TRANSCRIPTION_STATUSES = frozenset({
    "transcribing_queued", "transcribing",
    "separation_queued", "separating", "separation_ready",
})
# Lyrics review states.  Approved lyrics must be reopened through their own
# flow first: a re-transcription would silently invalidate the approval.
RETRANSCRIBABLE_STATUSES = frozenset({
    "transcribed_pending", "transcribed", "transcription_failed",
})


class RetranscriptionRejected(Exception):
    def __init__(self, status_code: int, code: str, message: str, **extra: Any):
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.extra = extra

    def payload(self) -> dict:
        return {"code": self.code, "message": self.message, **self.extra}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def previous_language(job) -> str | None:
    quality = job.transcription_quality if isinstance(job.transcription_quality, dict) else {}
    metrics = quality.get("metrics") if isinstance(quality.get("metrics"), dict) else {}
    value = str(metrics.get("language") or quality.get("language") or "").strip().lower()
    return value[:16] or None


def latest(db, job_id: str):
    from database import EditorRetranscription
    return (
        db.query(EditorRetranscription)
        .filter(EditorRetranscription.job_id == job_id)
        .order_by(EditorRetranscription.created_at.desc(), EditorRetranscription.id.desc())
        .first()
    )


def for_attempt(db, job_id: str, attempt_id: str | None, *, lock: bool = False):
    if not attempt_id:
        return None
    from database import EditorRetranscription
    query = db.query(EditorRetranscription).filter(
        EditorRetranscription.job_id == job_id,
        EditorRetranscription.attempt_id == str(attempt_id),
    )
    if lock:
        query = query.populate_existing().with_for_update()
    return query.first()


def settle_failed(db, job) -> bool:
    """Return a failed re-transcription to the editable previous version.

    The worker, the RQ failure callback and the reaper all mark the job
    ``transcription_failed``.  For a re-transcription that would strand a job
    whose previous text is still intact, so the job goes back to review and
    the archive row records the failure.  Caller holds the Job row lock and
    commits.
    """
    if job.status != "transcription_failed":
        return False
    row = for_attempt(db, job.job_id, job.active_transcription_attempt_id, lock=True)
    if row is None or row.status not in {"pending", "failed"}:
        return False
    if row.status == "pending":
        row.status = "failed"
        row.error = str(job.error or "transcription_failed")[:500]
        row.completed_at = _now()
    job.status = "transcribed_pending"
    job.current_step = "editing"
    job.progress = 100
    job.error = None
    db.flush()
    return True


def _checkpoint_current_text(db, job, document, user_id: int | None) -> str | None:
    """Make sure the text on screen is an immutable version before re-hearing."""
    from database import EditorVersion
    from editor import _ensure_version, _next_revision

    revision = int(document.revision or 0)
    existing = db.query(EditorVersion).filter(
        EditorVersion.job_id == job.job_id,
        EditorVersion.revision == revision,
    ).first()
    if existing is not None and existing.segments == document.current_segments:
        return existing.id
    revision = _next_revision(db, document, int(job.segments_revision or 0))
    version = _ensure_version(
        db, document, revision, list(document.current_segments or []), user_id,
        PRE_RETRANSCRIPTION_REASON,
    )
    document.revision = revision
    job.segments_revision = revision
    job.segments_json = list(document.current_segments or [])
    return version.id


def start(
    db,
    job,
    *,
    user_id: int | None,
    language: str,
    base_revision: int,
    audio_path: str,
    transcription_kwargs: dict,
    request_fingerprint: str = "",
):
    """Archive the current editor state and enqueue the forced-language attempt.

    The caller holds the Job row lock (``with_for_update``), commits, and
    dispatches the returned outbox event after the commit.
    Returns ``(retranscription_row, outbox_event)``.
    """
    from database import AuditLog, EditorDocument, EditorRetranscription

    settle_failed(db, job)
    language = str(language or "").strip().lower()
    if language not in SUPPORTED_LANGUAGES:
        raise RetranscriptionRejected(
            422, "unsupported_language", "Elegí un idioma de la lista.",
            supported=sorted(SUPPORTED_LANGUAGES),
        )
    current_revision = int(job.segments_revision or 0)
    if int(base_revision) != current_revision:
        raise RetranscriptionRejected(
            409, "stale_revision",
            "La versión guardada cambió. Revisá la letra actual y volvé a intentar.",
            current_revision=current_revision,
        )
    pending = db.query(EditorRetranscription.id).filter(
        EditorRetranscription.job_id == job.job_id,
        EditorRetranscription.status == "pending",
    ).first()
    if job.status in ACTIVE_TRANSCRIPTION_STATUSES or pending is not None:
        raise RetranscriptionRejected(
            409, "transcription_in_progress",
            "Esta canción ya se está transcribiendo. Esperá a que termine.",
            job_status=job.status,
        )
    if job.status not in RETRANSCRIBABLE_STATUSES or job.approved_at is not None:
        raise RetranscriptionRejected(
            409, "not_retranscribable",
            "La letra de esta canción ya no está en revisión; no se puede volver a transcribir.",
            job_status=job.status,
        )
    if not job.input_r2_key or not job.filename:
        raise RetranscriptionRejected(
            409, "audio_unavailable",
            "No encontramos el audio original de esta canción.",
        )

    document = None
    archived_version_id = None
    if job.segments_json:
        from editor import get_or_create_document
        document = get_or_create_document(
            db, job.job_id, job.tenant_id, job.segments_json or [],
        )
        archived_version_id = _checkpoint_current_text(db, job, document, user_id)
    else:
        document = db.query(EditorDocument).filter(
            EditorDocument.job_id == job.job_id,
        ).populate_existing().with_for_update().first()

    from transactional_outbox import create_transcription_outbox_event
    job.status = "transcribing_queued"
    job.current_step = "transcribe.prepare"
    job.progress = 1
    job.error = None
    job.last_progress_at = _now()
    event = create_transcription_outbox_event(
        db,
        job=job,
        audio_path=audio_path,
        request_fingerprint=request_fingerprint,
        transcription_kwargs=dict(transcription_kwargs, language=language),
    )
    row = EditorRetranscription(
        id=str(uuid.uuid4()),
        job_id=job.job_id,
        tenant_id=str(job.tenant_id or ""),
        attempt_id=event.id,
        requested_language=language,
        previous_language=previous_language(job),
        base_revision=int(job.segments_revision or 0),
        archived_version_id=archived_version_id,
        archived_original_segments=(
            document.original_segments if document is not None
            else (job.segments_json or None)
        ),
        archived_machine_evidence=(
            document.machine_evidence if document is not None else None
        ),
        archived_transcription_quality=(
            dict(job.transcription_quality)
            if isinstance(job.transcription_quality, dict) else None
        ),
        status="pending",
        requested_by=user_id,
        created_at=_now(),
    )
    db.add(row)
    db.add(AuditLog(
        user_id=user_id,
        action=AUDIT_ACTION,
        detail={
            "job_id": job.job_id,
            "retranscription_id": row.id,
            "attempt_id": event.id,
            "previous_language": row.previous_language,
            "requested_language": language,
            "base_revision": row.base_revision,
            "archived_version_id": archived_version_id,
            "had_machine_evidence": bool(row.archived_machine_evidence),
        },
    ))
    db.flush()
    return row, event


def install_baseline(db, job, row, segments: list[dict]) -> int:
    """Install a forced-language result as a new revision and new baseline.

    Runs inside the worker's persistence transaction, after the caller checked
    that ``row`` belongs to the job's active attempt and that no human edit
    landed since the request.  The previous baseline is already archived in
    ``row``; the previous human text is already an ``editor_versions`` row.
    Returns the revision the worker must persist the quality verdict for.
    """
    from database import EditorDocument
    from editor import _ensure_version, _next_revision, normalize_segments, now_utc

    # Never carry a human language attestation (or any verdict) of the old
    # text over to the new one.
    job.transcription_quality = {}
    document = db.query(EditorDocument).filter(
        EditorDocument.job_id == job.job_id,
    ).populate_existing().with_for_update().first()
    if document is None:
        # Untouched job: the regular first-snapshot path creates the document.
        return int(job.segments_revision or 0)
    normalized = normalize_segments(segments)
    revision = _next_revision(db, document, int(job.segments_revision or 0))
    _ensure_version(
        db, document, revision, normalized, None, RETRANSCRIPTION_REASON,
        provenance={
            "retranscription_id": row.id,
            "requested_language": row.requested_language,
            "previous_language": row.previous_language,
            "base_revision": int(row.base_revision),
        },
    )
    document.original_segments = normalized
    document.current_segments = normalized
    document.machine_evidence = None
    document.quality_proposal = None
    document.revision = revision
    document.updated_at = now_utc()
    job.segments_json = normalized
    job.segments_revision = revision
    db.flush()
    return revision


def mark_applied(row, revision: int) -> None:
    row.status = "applied"
    row.result_revision = int(revision)
    row.error = None
    row.completed_at = _now()


def mark_discarded(job, row, reason: str) -> None:
    """The result was not installed; the previous text stays current."""
    if row.status == "pending":
        row.status = "discarded"
        row.error = str(reason)[:500]
        row.completed_at = _now()
    if job.status in ACTIVE_TRANSCRIPTION_STATUSES:
        job.status = "transcribed_pending"
        job.current_step = "editing"
        job.progress = 100


def status_payload(job, row) -> dict:
    if row is None:
        return {"status": "idle", "job_status": job.status}
    status = row.status
    if status == "pending":
        status = "queued" if job.status == "transcribing_queued" else "running"
    return {
        "status": status,
        "retranscription_id": row.id,
        "language": row.requested_language,
        "previous_language": row.previous_language,
        "base_revision": int(row.base_revision),
        "result_revision": row.result_revision,
        "error": row.error if row.status in {"failed", "discarded"} else None,
        "job_status": job.status,
        "current_step": job.current_step,
        "progress": int(job.progress or 0),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
    }
