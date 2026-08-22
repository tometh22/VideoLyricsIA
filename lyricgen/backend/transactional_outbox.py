"""Transactional publication for stateful jobs.

The database mutation and this intent are committed together.  Redis/RQ is
then a delivery mechanism, not the source of truth, so an ambiguous enqueue
timeout never requires rewinding an editor revision (the classic ABA bug).
"""

from __future__ import annotations

import uuid
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Callable


OUTBOX_CONSUMER_MIN_LEASE_SECONDS = 3900
OUTBOX_DEFAULT_MAX_ATTEMPTS = 12


class OutboxDeliveryError(RuntimeError):
    """A classified publication failure.

    Outbox rows are durable state, so storing only ``RuntimeError`` makes an
    incident impossible to diagnose and causes non-retryable rollout/config
    results to spin forever.  Keep the public exception small and safe: the
    code is persisted, while the original exception remains available to the
    caller/logging path.
    """

    def __init__(self, code: str, *, retryable: bool = True):
        self.code = str(code)[:96] or "outbox_delivery_failed"
        self.retryable = bool(retryable)
        super().__init__(self.code)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    # SQLite drops timezone metadata even for timezone=True columns.  The
    # persisted value was written in UTC, so restore that contract on read.
    return value.replace(tzinfo=timezone.utc)


def _outbox_consumer_lease_seconds() -> int:
    """Keep one edit claim exclusive beyond the 3600-second RQ timeout."""
    try:
        configured = int(os.environ.get(
            "JOB_OUTBOX_CONSUMER_LEASE_SECONDS",
            str(OUTBOX_CONSUMER_MIN_LEASE_SECONDS),
        ))
    except (TypeError, ValueError):
        configured = OUTBOX_CONSUMER_MIN_LEASE_SECONDS
    return max(OUTBOX_CONSUMER_MIN_LEASE_SECONDS, configured)


def _outbox_stale_processing_seconds() -> int:
    """Never let the reconciler steal a claim before its consumer lease."""
    try:
        configured = int(os.environ.get(
            "JOB_OUTBOX_STALE_PROCESSING_SECONDS",
            str(OUTBOX_CONSUMER_MIN_LEASE_SECONDS),
        ))
    except (TypeError, ValueError):
        configured = OUTBOX_CONSUMER_MIN_LEASE_SECONDS
    return max(_outbox_consumer_lease_seconds(), configured)


def _outbox_max_attempts() -> int:
    """Bound transient outbox retries so a broken dependency cannot spin.

    Twelve attempts is long enough to cover a rolling deploy or a short R2 /
    Redis incident while still producing a visible terminal state within a
    bounded window. Operators can raise it for a controlled recovery.
    """
    try:
        configured = int(os.environ.get(
            "JOB_OUTBOX_MAX_ATTEMPTS", str(OUTBOX_DEFAULT_MAX_ATTEMPTS),
        ))
    except (TypeError, ValueError):
        configured = OUTBOX_DEFAULT_MAX_ATTEMPTS
    return max(1, min(configured, 100))


def _outbox_error_code(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    if code:
        return str(code)[:140]
    # Generic dependency exceptions are deliberately classified without
    # persisting their messages, which may contain provider/request details.
    return type(exc).__name__[:140] or "outbox_delivery_failed"


def create_outbox_event(
    db,
    *,
    job_id: str,
    event_type: str,
    dedupe_key: str,
    payload: dict[str, Any],
):
    from database import JobOutboxEvent

    existing = db.query(JobOutboxEvent).filter(
        JobOutboxEvent.dedupe_key == str(dedupe_key)[:160],
    ).first()
    if existing is not None:
        return existing
    event = JobOutboxEvent(
        id=str(uuid.uuid4()),
        job_id=job_id,
        event_type=str(event_type)[:64],
        dedupe_key=str(dedupe_key)[:160],
        payload=dict(payload),
        status="pending",
        attempts=0,
        available_at=_now(),
        created_at=_now(),
    )
    db.add(event)
    db.flush()
    return event


def create_quality_outbox_event(
    db,
    *,
    job,
    revision: int,
    segments: list[dict],
    quality: dict | None,
    reason: str,
):
    """Persist a content-free quality publication intent with the mutation."""
    if not isinstance(quality, dict) or not quality.get("unsafe_windows"):
        return None
    from transcription_quality import segments_hash

    snapshot_hash = segments_hash(segments)
    audio_revision = int(getattr(job, "audio_revision", 0) or 0)
    audio_sha256 = str(getattr(job, "input_audio_sha256", "") or "").lower()
    return create_outbox_event(
        db,
        job_id=job.job_id,
        event_type="quality.enqueue",
        dedupe_key=(
            f"quality:{job.job_id}:{int(revision)}:{snapshot_hash[:20]}:"
            f"{audio_revision}:{audio_sha256[:20]}"
        ),
        payload={
            "expected_revision": int(revision),
            "expected_segments_hash": snapshot_hash,
            "expected_audio_revision": audio_revision,
            "expected_audio_sha256": audio_sha256,
            "filename": str(getattr(job, "filename", "") or ""),
            "tenant_id": str(getattr(job, "tenant_id", "") or ""),
            "reason": str(reason)[:64],
            "allow_audio_identity_backfill": True,
        },
    )


def create_pipeline_outbox_event(
    db,
    *,
    job,
    purpose: str,
    mp3_path: str | None,
    artist: str,
    style: str,
    plan: str,
    tenant_id: str,
    pipeline_kwargs: dict[str, Any],
):
    """Commit-ready intent for a render pipeline invocation."""
    event = create_outbox_event(
        db,
        job_id=job.job_id,
        event_type="pipeline.enqueue",
        dedupe_key=f"pipeline:{job.job_id}:{uuid.uuid4()}",
        payload={
            "purpose": str(purpose)[:32],
            "mp3_path": mp3_path,
            "artist": artist,
            "style": style,
            "plan": plan,
            "tenant_id": tenant_id,
            "pipeline_kwargs": dict(pipeline_kwargs),
        },
    )
    job.active_pipeline_attempt_id = event.id
    return event


def create_transcription_outbox_event(
    db,
    *,
    job,
    audio_path: str,
    transcription_kwargs: dict[str, Any],
):
    """Commit-ready intent for a transcription invocation."""
    event = create_outbox_event(
        db,
        job_id=job.job_id,
        event_type="transcription.enqueue",
        dedupe_key=f"transcription:{job.job_id}:{uuid.uuid4()}",
        payload={
            "audio_path": audio_path,
            "transcription_kwargs": dict(transcription_kwargs),
        },
    )
    job.active_transcription_attempt_id = event.id
    return event


def _publish(
    event,
    *,
    edit_publisher: Callable[..., str | None] | None = None,
    pipeline_publisher: Callable[..., str | None] | None = None,
    transcription_publisher: Callable[..., str | None] | None = None,
) -> str | None:
    payload = dict(event.payload or {})
    if event.event_type == "edit.enqueue":
        if edit_publisher is None:
            from queue_jobs import enqueue_edit as edit_publisher

        return edit_publisher(
            job_id=event.job_id,
            edit_type=str(payload["edit_type"]),
            edit_params=dict(payload.get("edit_params") or {}),
            plan=str(payload.get("plan") or "100"),
            tenant_id=str(payload.get("tenant_id") or ""),
            publication_id=str(event.id),
            publication_dedupe_key=str(event.dedupe_key),
        )
    if event.event_type == "quality.enqueue":
        from queue_jobs import (
            _valid_quality_audio_identity,
            enqueue_transcription_quality,
            ensure_legacy_audio_identity,
        )

        audio_revision = int(payload.get("expected_audio_revision") or 0)
        audio_sha256 = str(payload.get("expected_audio_sha256") or "").lower()
        if not _valid_quality_audio_identity(audio_revision, audio_sha256):
            identity = ensure_legacy_audio_identity(event.job_id)
            if not identity:
                raise OutboxDeliveryError("audio_identity_backfill_pending")
            audio_revision = int(identity["audio_revision"])
            audio_sha256 = str(identity["audio_sha256"])
        result = enqueue_transcription_quality(
            event.job_id,
            expected_revision=int(payload["expected_revision"]),
            expected_segments_hash=str(payload["expected_segments_hash"]),
            filename=str(payload.get("filename") or ""),
            tenant_id=str(payload.get("tenant_id") or ""),
            expected_audio_revision=audio_revision,
            expected_audio_sha256=audio_sha256,
            publication_id=str(event.id),
            publication_dedupe_key=str(event.dedupe_key),
        )
        if str(result).startswith(("disabled:", "rollout-excluded:")):
            # Quality analysis is advisory. A disabled or excluded rollout is
            # a deliberate terminal outcome, not a transient delivery error.
            raise OutboxDeliveryError(
                str(result).split(":", 1)[0] + "_quality_delivery",
                retryable=False,
            )
        if str(result).startswith(("rollout:", "identity-missing:")):
            raise OutboxDeliveryError(
                str(result).split(":", 1)[0] + "_quality_delivery",
            )
        return result
    if event.event_type == "pipeline.enqueue":
        if pipeline_publisher is None:
            from queue_jobs import enqueue_pipeline as pipeline_publisher

        return pipeline_publisher(
            job_id=event.job_id,
            mp3_path=payload.get("mp3_path"),
            artist=str(payload.get("artist") or ""),
            style=str(payload.get("style") or "oscuro"),
            plan=str(payload.get("plan") or "100"),
            tenant_id=str(payload.get("tenant_id") or ""),
            publication_id=str(event.id),
            publication_dedupe_key=str(event.dedupe_key),
            **dict(payload.get("pipeline_kwargs") or {}),
        )
    if event.event_type == "transcription.enqueue":
        if transcription_publisher is None:
            from queue_jobs import enqueue_transcription as transcription_publisher

        return transcription_publisher(
            event.job_id,
            str(payload.get("audio_path") or ""),
            publication_id=str(event.id),
            publication_dedupe_key=str(event.dedupe_key),
            **dict(payload.get("transcription_kwargs") or {}),
        )
    raise OutboxDeliveryError(
        f"unsupported_event_type:{event.event_type}", retryable=False,
    )


def dispatch_outbox_event(
    event_id: str,
    *,
    raise_on_error: bool = False,
    edit_publisher: Callable[..., str | None] | None = None,
    pipeline_publisher: Callable[..., str | None] | None = None,
    transcription_publisher: Callable[..., str | None] | None = None,
) -> dict:
    from database import JobOutboxEvent, SessionLocal

    db = SessionLocal()
    try:
        event = db.query(JobOutboxEvent).filter(
            JobOutboxEvent.id == event_id,
        ).with_for_update().first()
        if event is None:
            return {"status": "missing", "event_id": event_id}
        if event.status in {"dispatched", "processing", "consumed", "failed", "skipped"}:
            return {"status": event.status, "event_id": event_id, "deduplicated": True}
        if _aware(event.available_at) and _aware(event.available_at) > _now():
            return {"status": "deferred", "event_id": event_id}
        try:
            rq_id = _publish(
                event,
                edit_publisher=edit_publisher,
                pipeline_publisher=pipeline_publisher,
                transcription_publisher=transcription_publisher,
            )
        except Exception as exc:
            event.attempts = int(event.attempts or 0) + 1
            error_code = _outbox_error_code(exc)
            retryable = bool(getattr(exc, "retryable", True))
            exhausted = event.attempts >= _outbox_max_attempts()
            if retryable and not exhausted:
                event.status = "pending"
                event.last_error = error_code
                event.available_at = _now() + timedelta(
                    seconds=min(300, max(5, 2 ** min(event.attempts, 8))),
                )
            else:
                # ``skipped`` is intentional for advisory quality rollouts;
                # ``failed`` means a retryable dependency never recovered.
                event.status = "failed" if retryable else "skipped"
                event.last_error = (
                    f"{error_code}:attempts={event.attempts}"
                )[:160]
                event.available_at = _now()
            db.commit()
            if raise_on_error:
                raise
            return {
                "status": event.status, "event_id": event_id,
                "error": error_code, "attempts": event.attempts,
            }
        event.status = "dispatched"
        event.dispatched_at = _now()
        event.last_error = None
        event.attempts = int(event.attempts or 0) + 1
        db.commit()
        return {"status": "dispatched", "event_id": event_id, "rq_job_id": rq_id}
    finally:
        db.close()


def dispatch_pending_outbox_events(*, limit: int = 50) -> dict:
    from database import JobOutboxEvent, SessionLocal

    db = SessionLocal()
    try:
        # A worker can disappear after claiming but before RQ records failure.
        # Do not strand that event forever; the threshold exceeds the edit
        # job timeout so a legitimately long render cannot be stolen.
        stale_seconds = _outbox_stale_processing_seconds()
        stale_cutoff = _now() - timedelta(seconds=stale_seconds)
        stale = db.query(JobOutboxEvent).filter(
            JobOutboxEvent.status == "processing",
            JobOutboxEvent.processing_at < stale_cutoff,
        ).with_for_update().all()
        for event in stale:
            event.status = "pending"
            event.processing_at = None
            event.processing_token = None
            event.available_at = _now()
            event.last_error = "stale_consumer_recovered"
        if stale:
            db.commit()
        ids = [row.id for row in (
            db.query(JobOutboxEvent)
            .filter(
                JobOutboxEvent.status == "pending",
                JobOutboxEvent.available_at <= _now(),
            )
            .order_by(JobOutboxEvent.created_at.asc())
            .limit(max(1, min(int(limit), 200)))
            .all()
        )]
    finally:
        db.close()
    results = [dispatch_outbox_event(event_id) for event_id in ids]
    return {
        "attempted": len(results),
        "dispatched": sum(item.get("status") == "dispatched" for item in results),
        "pending": sum(item.get("status") == "pending" for item in results),
        "failed": sum(item.get("status") == "failed" for item in results),
        "skipped": sum(item.get("status") == "skipped" for item in results),
    }


def _claim_outbox_consumer(
    event_id: str, dedupe_key: str, token: str, *, lease_seconds: int | None = None,
) -> str:
    """Claim one event execution; stale claims are recoverable by RQ retry."""
    from database import JobOutboxEvent, SessionLocal

    effective_lease = max(
        OUTBOX_CONSUMER_MIN_LEASE_SECONDS,
        int(lease_seconds) if lease_seconds is not None
        else _outbox_consumer_lease_seconds(),
    )

    db = SessionLocal()
    try:
        event = db.query(JobOutboxEvent).filter(
            JobOutboxEvent.id == event_id,
        ).with_for_update().first()
        if event is None or str(event.dedupe_key) != str(dedupe_key):
            return "invalid"
        if event.status == "consumed":
            return "consumed"
        processing_at = _aware(event.processing_at)
        if (
            event.status == "processing"
            and processing_at is not None
            and processing_at >= _now() - timedelta(seconds=effective_lease)
        ):
            return "busy"
        event.status = "processing"
        event.processing_at = _now()
        event.processing_token = token
        event.attempts = int(event.attempts or 0) + 1
        db.commit()
        return "claimed"
    finally:
        db.close()


def _finish_outbox_consumer(
    event_id: str, dedupe_key: str, token: str, *, success: bool,
) -> bool:
    from database import JobOutboxEvent, SessionLocal

    db = SessionLocal()
    try:
        event = db.query(JobOutboxEvent).filter(
            JobOutboxEvent.id == event_id,
        ).with_for_update().first()
        if (
            event is None
            or str(event.dedupe_key) != str(dedupe_key)
            or str(event.processing_token or "") != str(token)
        ):
            return False
        event.processing_at = None
        event.processing_token = None
        if success:
            event.status = "consumed"
            event.consumed_at = _now()
            event.last_error = None
        else:
            # The deterministic RQ job is still the delivery identity. A
            # reconciler can safely rediscover it or publish it after a crash.
            event.status = "pending"
            event.available_at = _now()
            event.last_error = "consumer_failed"
        db.commit()
        return True
    finally:
        db.close()


def run_outbox_edit_pipeline(
    job_id: str,
    event_id: str,
    dedupe_key: str,
    edit_type: str,
    edit_params: dict,
    policy_fingerprint: str,
):
    """Idempotent event-scoped adapter around the existing edit pipeline."""
    token = str(uuid.uuid4())
    lease = _outbox_consumer_lease_seconds()
    claim = _claim_outbox_consumer(
        event_id, dedupe_key, token, lease_seconds=lease,
    )
    if claim == "consumed":
        return {"status": "already_consumed", "event_id": event_id}
    if claim == "busy":
        raise RuntimeError("outbox_consumer_busy")
    if claim != "claimed":
        raise RuntimeError("outbox_consumer_identity_mismatch")
    try:
        from pipeline import run_edit_pipeline
        result = run_edit_pipeline(
            job_id, edit_type, dict(edit_params or {}), policy_fingerprint,
        )
    except BaseException:
        _finish_outbox_consumer(
            event_id, dedupe_key, token, success=False,
        )
        raise
    _finish_outbox_consumer(event_id, dedupe_key, token, success=True)
    return result


def _run_outbox_job(
    *,
    kind: str,
    job_id: str,
    event_id: str,
    dedupe_key: str,
    callback: Callable[[], Any],
):
    token = str(uuid.uuid4())
    claim = _claim_outbox_consumer(event_id, dedupe_key, token)
    if claim == "consumed":
        return {"status": "already_consumed", "event_id": event_id}
    if claim == "busy":
        raise RuntimeError("outbox_consumer_busy")
    if claim != "claimed":
        raise RuntimeError("outbox_consumer_identity_mismatch")
    try:
        from jobs import bind_job_attempt
        with bind_job_attempt(kind, event_id):
            result = callback()
    except BaseException:
        _finish_outbox_consumer(event_id, dedupe_key, token, success=False)
        raise
    _finish_outbox_consumer(event_id, dedupe_key, token, success=True)
    return result


def run_outbox_pipeline(
    job_id: str,
    event_id: str,
    dedupe_key: str,
    mp3_path: str | None,
    artist: str,
    style: str,
    pipeline_kwargs: dict,
):
    def _call():
        from pipeline import run_pipeline
        return run_pipeline(
            job_id, mp3_path, artist, style, **dict(pipeline_kwargs or {}),
        )

    return _run_outbox_job(
        kind="pipeline", job_id=job_id, event_id=event_id,
        dedupe_key=dedupe_key, callback=_call,
    )


def run_outbox_transcription(
    job_id: str,
    event_id: str,
    dedupe_key: str,
    audio_path: str,
    transcription_kwargs: dict,
):
    def _call():
        from transcription_worker import run_transcription_job
        return run_transcription_job(
            job_id, audio_path, **dict(transcription_kwargs or {}),
        )

    return _run_outbox_job(
        kind="transcription", job_id=job_id, event_id=event_id,
        dedupe_key=dedupe_key, callback=_call,
    )


def reconcile_job_outbox() -> dict:
    """RQ entry point; always arranges a successor wake-up."""
    try:
        return dispatch_pending_outbox_events()
    finally:
        try:
            from queue_jobs import ensure_job_outbox_reconciler_scheduled
            ensure_job_outbox_reconciler_scheduled()
        except Exception:
            pass
