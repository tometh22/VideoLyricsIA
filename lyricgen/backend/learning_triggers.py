"""Fail-closed corpus milestones and research-job triggers.

This module deliberately keeps orchestration separate from the transcription
path.  A completed approval may request a research run, but it can never
mutate lyrics or replace a production ASR family.  Milestones are persisted in
``audit_log`` so a worker restart or a duplicated approval cannot enqueue the
same bucket twice.
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger("genly.learning_triggers")
_TRUE = {"1", "true", "yes", "on"}

TRIGGER_SPECS: dict[str, dict[str, Any]] = {
    "lora_retraining": {
        "threshold_env": "CORPUS_RETRAIN_EVERY_SONGS",
        "threshold_default": 100,
        "enabled_env": "LORA_V1_AUTORETRAIN_ENABLED",
        "job_function": "run_lora_retraining_trigger",
    },
    "realignment_selector": {
        "threshold_env": "REALIGN_SELECTOR_TRIGGER_SONGS",
        "threshold_default": 200,
        "enabled_env": "REALIGN_SELECTOR_AUTORUN_ENABLED",
        "job_function": "run_realign_selector_trigger",
    },
    "agent_d1": {
        "threshold_env": "AGENT_D1_TRIGGER_SONGS",
        "threshold_default": 100,
        "enabled_env": "AGENT_D1_AUTORUN_ENABLED",
        "job_function": "run_agent_d1_trigger",
    },
}


def env_enabled(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUE


def _threshold(spec: dict[str, Any]) -> int:
    try:
        value = int(os.environ.get(spec["threshold_env"], spec["threshold_default"]))
    except (TypeError, ValueError):
        value = int(spec["threshold_default"])
    return max(1, value)


def catalog_training_authorization() -> dict[str, Any]:
    """Return the explicit runtime authorization without exposing secrets.

    The boolean is the kill switch.  An optional non-secret reference is
    recorded in every run for auditability; it is never used as a credential.
    """
    enabled = env_enabled("CATALOG_AUDIO_TRAINING_ENABLED")
    reference = os.environ.get("CATALOG_AUDIO_TRAINING_AUTHORIZATION_ID", "").strip()
    return {
        "enabled": enabled,
        "authorized": enabled,
        "authorization_reference_present": bool(reference),
        "authorization_reference": reference or None,
        "reason": None if enabled else "CATALOG_AUDIO_TRAINING_ENABLED is off",
    }


def corpus_song_count(db) -> int:
    """Count distinct approved jobs with immutable machine evidence."""
    from sqlalchemy import func
    from database import EditorVersion, Job

    value = db.query(func.count(func.distinct(Job.job_id))).join(
        EditorVersion, EditorVersion.job_id == Job.job_id,
    ).filter(
        Job.machine_snapshot_required.is_(True),
        EditorVersion.is_approved.is_(True),
    ).scalar()
    return int(value or 0)


def _scheduled_buckets(db, trigger_type: str) -> set[int]:
    from database import AuditLog

    rows = db.query(AuditLog).filter(
        AuditLog.action == "learning_trigger.scheduled",
    ).order_by(AuditLog.id.desc()).limit(5000).all()
    buckets: set[int] = set()
    for row in rows:
        detail = row.detail if isinstance(row.detail, dict) else {}
        if str(detail.get("trigger_type") or "") != trigger_type:
            continue
        try:
            buckets.add(int(detail["bucket"]))
        except (KeyError, TypeError, ValueError):
            continue
    return buckets


def _enqueue_research_job(trigger_type: str, bucket: int, count: int) -> str:
    """Enqueue one deterministic RQ job and return its id.

    Redis unavailability is returned to the caller as a blocked outcome; no
    thread fallback is allowed for training/evaluation work.
    """
    import queue_jobs

    if not queue_jobs.transcription_quality_queue_enabled():
        raise RuntimeError("TRANSCRIPTION_QUALITY_QUEUE_ENABLED is off")
    queue_jobs._init_redis()
    if queue_jobs._redis is None:
        raise RuntimeError("transcription_quality Redis unavailable")
    from rq import Queue

    spec = TRIGGER_SPECS[trigger_type]
    rq_id = f"research-trigger:{trigger_type}:{bucket}"
    active = queue_jobs._active_rq_job(queue_jobs._redis, rq_id)
    if active is not None:
        return active.id
    queue_jobs._evict_stale_rq_job(queue_jobs._redis, rq_id)
    function = getattr(__import__("learning_triggers"), spec["job_function"])
    queued = Queue("transcription_quality", connection=queue_jobs._redis).enqueue(
        function, args=(bucket, count), job_timeout=int(os.environ.get(
            "RESEARCH_TRIGGER_JOB_TIMEOUT", "21600",
        )), result_ttl=queue_jobs.RESULT_TTL, failure_ttl=queue_jobs.FAILURE_TTL,
        job_id=rq_id,
        meta=queue_jobs.rq_payload_metadata(
            "research_trigger", trigger_type=trigger_type, bucket=bucket,
        ),
    )
    return queued.id


def schedule_due_triggers(*, reason: str = "approval") -> dict[str, Any]:
    """Schedule each enabled milestone once and record a durable audit row."""
    from database import AuditLog, SessionLocal

    db = SessionLocal()
    count = 0
    result: dict[str, Any] = {"reason": reason, "scheduled": [], "blocked": []}
    try:
        count = corpus_song_count(db)
        result["corpus_songs"] = count
        authorization = catalog_training_authorization()
        for trigger_type, spec in TRIGGER_SPECS.items():
            enabled = env_enabled(spec["enabled_env"])
            if trigger_type == "lora_retraining" and not authorization["authorized"]:
                result["blocked"].append({
                    "trigger_type": trigger_type,
                    "reason": authorization["reason"],
                })
                continue
            if not enabled:
                continue
            threshold = _threshold(spec)
            due_bucket = count // threshold
            if due_bucket < 1:
                continue
            scheduled = _scheduled_buckets(db, trigger_type)
            for bucket in range(1, due_bucket + 1):
                if bucket in scheduled:
                    continue
                try:
                    rq_id = _enqueue_research_job(trigger_type, bucket, count)
                except Exception as exc:
                    result["blocked"].append({
                        "trigger_type": trigger_type, "bucket": bucket,
                        "reason": type(exc).__name__,
                    })
                    continue
                detail = {
                    "trigger_type": trigger_type, "bucket": bucket,
                    "threshold": threshold, "corpus_songs": count,
                    "rq_job_id": rq_id, "reason": reason,
                    "authorization": authorization if trigger_type == "lora_retraining" else None,
                }
                db.add(AuditLog(
                    user_id=None, action="learning_trigger.scheduled", detail=detail,
                ))
                result["scheduled"].append(detail)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _run_configured_command(kind: str, bucket: int, count: int) -> dict[str, Any]:
    """Run an explicitly configured offline executor, never an implicit shell.

    The worker image does not contain the 2.6GB private golden set.  A trigger
    therefore records ``blocked_executor_missing`` until an operator-mounted,
    absolute executable is configured.  This prevents a scheduled job from
    claiming that training/evaluation happened when it did not.
    """
    import json
    import subprocess

    command = os.environ.get(f"{kind.upper()}_EXECUTOR", "").strip()
    if not command:
        return {
            "status": "blocked_executor_missing", "trigger_type": kind,
            "bucket": bucket, "corpus_songs": count,
        }
    if not os.path.isabs(command):
        return {"status": "blocked_executor_not_absolute", "trigger_type": kind}
    try:
        completed = subprocess.run(
            [command, "--trigger", kind, "--bucket", str(bucket),
             "--corpus-songs", str(count)],
            check=False, capture_output=True, text=True,
            timeout=int(os.environ.get("RESEARCH_TRIGGER_EXECUTOR_TIMEOUT", "21600")),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "executor_error", "trigger_type": kind,
                "error_type": type(exc).__name__}
    payload: dict[str, Any] = {
        "status": "completed" if completed.returncode == 0 else "executor_failed",
        "trigger_type": kind, "bucket": bucket, "returncode": completed.returncode,
    }
    try:
        output = json.loads(completed.stdout or "{}")
        if isinstance(output, dict):
            payload["executor_report"] = output
    except (TypeError, ValueError):
        payload["executor_stdout_present"] = bool(completed.stdout)
    return payload


def run_lora_retraining_trigger(bucket: int, corpus_songs: int) -> dict[str, Any]:
    return _run_configured_command("lora_v1", bucket, corpus_songs)


def run_realign_selector_trigger(bucket: int, corpus_songs: int) -> dict[str, Any]:
    return _run_configured_command("realign_selector", bucket, corpus_songs)


def run_agent_d1_trigger(bucket: int, corpus_songs: int) -> dict[str, Any]:
    return _run_configured_command("agent_d1", bucket, corpus_songs)


def trigger_after_capture() -> dict[str, Any]:
    """Best-effort milestone hook; capture success never depends on it."""
    try:
        return schedule_due_triggers(reason="correction_capture")
    except Exception as exc:
        logger.warning("[LEARNING-TRIGGERS] hook failed error_type=%s", type(exc).__name__)
        return {"status": "trigger_hook_failed", "error_type": type(exc).__name__}


def run_learning_trigger_reconciler() -> dict[str, Any]:
    """Run one reconciliation pass and schedule the next wake-up."""
    result = schedule_due_triggers(reason="periodic_reconcile")
    try:
        from queue_jobs import ensure_learning_triggers_scheduled
        result["next_job_id"] = ensure_learning_triggers_scheduled()
    except Exception as exc:
        result["schedule_warning"] = type(exc).__name__
    return result
