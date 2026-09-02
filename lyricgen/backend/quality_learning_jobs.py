"""RQ entrypoints for the correction-learning feedback loop."""
from __future__ import annotations

import hashlib
import json
import os
import resource
import time
from datetime import datetime, timezone
from pathlib import Path


def run_correction_observation_job(job_id: str, approved_version_id: str,
                                   *, active_edit_ms: int | None = None,
                                   active_edit_source: str | None = None,
                                   source_confidence: str = "exact",
                                   session_hmac: str | None = None,
                                   expected_revision: int | None = None,
                                   expected_approved_hash: str | None = None,
                                   expected_learning_epoch: int | None = None) -> dict:
    from correction_learning import create_observation, StaleCorrectionSnapshot
    from database import SessionLocal
    started = time.monotonic()
    usage_before = resource.getrusage(resource.RUSAGE_SELF)
    db = SessionLocal()
    try:
        row = create_observation(
            db, job_id, approved_version_id,
            active_edit_ms=active_edit_ms,
            active_edit_source=active_edit_source,
            source_confidence=source_confidence,
            session_hmac=session_hmac,
            expected_revision=expected_revision,
            expected_approved_hash=expected_approved_hash,
            expected_learning_epoch=expected_learning_epoch,
        )
        metrics = dict(row.metrics or {})
        if "operational" not in metrics:
            usage_after = resource.getrusage(resource.RUSAGE_SELF)
            queue_wait_s = None
            retry_count = 0
            try:
                from rq import get_current_job
                rq_job = get_current_job()
                if rq_job is not None:
                    enqueued = getattr(rq_job, "enqueued_at", None)
                    if enqueued is not None:
                        if enqueued.tzinfo is None:
                            enqueued = enqueued.replace(tzinfo=timezone.utc)
                        queue_wait_s = max(
                            0.0, (datetime.now(timezone.utc) - enqueued).total_seconds(),
                        )
                    retry_count = int((getattr(rq_job, "meta", None) or {}).get("retry_count", 0))
            except Exception:
                pass
            metrics["operational"] = {
                "queue": "transcription_quality",
                "queue_wait_s": round(queue_wait_s, 3) if queue_wait_s is not None else None,
                "wall_s": round(time.monotonic() - started, 3),
                "cpu_user_s": round(usage_after.ru_utime - usage_before.ru_utime, 3),
                "cpu_system_s": round(usage_after.ru_stime - usage_before.ru_stime, 3),
                "max_rss_kb": int(usage_after.ru_maxrss),
                "retry_count": retry_count, "cache_hits": 0,
                "provider_tokens": 0, "audio_seconds_billed": 0.0,
                "api_cost_usd": 0.0,
            }
            row.metrics = metrics
        db.commit()
        # Milestone triggers are deliberately best-effort: the correction
        # observation is already durable, and a Redis/executor outage must not
        # turn a successful approval into a failed learning capture.
        try:
            from learning_triggers import trigger_after_capture
            trigger_after_capture()
        except Exception as exc:
            import logging
            logging.getLogger("genly.quality_learning").warning(
                "[LEARNING-TRIGGERS] post-capture hook failed error_type=%s",
                type(exc).__name__,
            )
        return {
            "observation_id": row.id, "label_tier": row.label_tier,
            "mutated_segments": False,
        }
    except StaleCorrectionSnapshot as exc:
        db.rollback()
        return {
            "status": "discarded", "reason": str(exc),
            "mutated_segments": False,
        }
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def run_daily_quality_learning() -> dict:
    from correction_learning import mature_observations, mine_patterns, model_readiness
    from database import SessionLocal
    if os.environ.get("QUALITY_LEARNING_MINING_ENABLED", "0").strip().lower() not in {
        "1", "true", "yes", "on",
    }:
        result = {"disabled": True}
        try:
            from queue_jobs import ensure_daily_quality_learning_scheduled
            result["next_job_id"] = ensure_daily_quality_learning_scheduled()
        except Exception as exc:
            result["schedule_warning"] = str(exc)[:200]
        return result
    db = SessionLocal()
    try:
        from database import AuditLog
        maturation = mature_observations(db)
        mining = mine_patterns(db)
        readiness = model_readiness(db)
        result = {
            "maturation": maturation, "mining": mining,
            "model_readiness": readiness, "mutated_segments": False,
        }
        db.add(AuditLog(
            user_id=None,
            action="quality_learning.mining.completed",
            detail=result,
        ))
        db.commit()
        try:
            from learning_triggers import trigger_after_capture
            result["learning_triggers"] = trigger_after_capture()
        except Exception as exc:
            result["learning_triggers"] = {
                "status": "trigger_hook_failed", "error_type": type(exc).__name__,
            }
        try:
            from queue_jobs import ensure_daily_quality_learning_scheduled
            result["next_job_id"] = ensure_daily_quality_learning_scheduled()
        except Exception as exc:
            result["schedule_warning"] = str(exc)[:200]
        return result
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _load_validation_report(path: Path) -> tuple[dict, str]:
    raw = path.read_bytes()
    report = json.loads(raw)
    if not isinstance(report, dict):
        raise ValueError("benchmark report must be an object")
    from evidence_attestation import verify_artifact
    verified, reason = verify_artifact(report, "BENCHMARK_RELEASE_PUBLIC_KEYS")
    if not verified:
        raise ValueError(f"benchmark report attestation failed: {reason}")
    return report, hashlib.sha256(raw).hexdigest()


def _validate_report(report: dict, proposal_id: str, candidate_hash: str) -> dict:
    """Validate a proposal-bound, no-render ablation result fail-closed."""
    binding = report.get("quality_learning") or {}
    if binding.get("proposal_id") != proposal_id:
        raise ValueError("benchmark report is not bound to this proposal")
    if binding.get("candidate_config_sha256") != candidate_hash:
        raise ValueError("benchmark report candidate configuration mismatch")
    if binding.get("render") is not False:
        raise ValueError("quality learning validation must not render")
    required = {
        "target_relative_reduction", "wer_delta_percentage_points",
        "temporal_integrity", "cost_delta_ci_high_usd", "one_variable_ablation",
        "baseline_config_sha256", "audio_manifest_sha256", "comparators",
        "veo_calls",
    }
    if not required.issubset(binding):
        raise ValueError("benchmark report lacks required ablation metrics")
    checks = {
        "target_reduction": float(binding["target_relative_reduction"]) >= 0.20,
        "wer_non_regression": float(binding["wer_delta_percentage_points"]) <= 1.0,
        "temporal_integrity": binding["temporal_integrity"] is True,
        "cost_favorable": float(binding["cost_delta_ci_high_usd"]) < 0.0,
        "one_variable_ablation": binding["one_variable_ablation"] is True,
        "v5_gate": (report.get("release_gate") or {}).get("decision") == "GO",
    }
    baseline_hash = str(binding["baseline_config_sha256"])
    if len(baseline_hash) != 64 or any(ch not in "0123456789abcdef" for ch in baseline_hash):
        raise ValueError("benchmark report baseline configuration hash is invalid")
    audio_manifest_hash = str(binding["audio_manifest_sha256"])
    if (
        len(audio_manifest_hash) != 64
        or any(ch not in "0123456789abcdef" for ch in audio_manifest_hash)
    ):
        raise ValueError("benchmark audio manifest hash is invalid")
    comparators = {str(value).casefold() for value in (binding["comparators"] or [])}
    if not {"baseline", "candidate", "rotor"}.issubset(comparators):
        raise ValueError("benchmark must compare baseline, candidate and ROTOR")
    if int(binding["veo_calls"]) != 0:
        raise ValueError("quality learning validation must not call Veo")
    metrics = {
        key: binding[key] for key in (
            "target_relative_reduction", "wer_delta_percentage_points",
            "temporal_integrity", "cost_delta_ci_high_usd", "one_variable_ablation",
        )
    }
    metrics["render"] = False
    metrics["veo_calls"] = 0
    metrics["audio_manifest_sha256"] = audio_manifest_hash
    metrics["comparators"] = ["baseline", "candidate", "ROTOR"]
    return {
        "checks": checks, "passed": all(checks.values()), "metrics": metrics,
        "baseline_config_sha256": baseline_hash,
    }


def run_quality_proposal_validation(proposal_id: str, experiment_id: str) -> dict:
    from correction_learning import env_enabled, sha256_json, validate_proposal_config
    from database import (
        AuditLog, QualityExperimentRun, QualityFixProposal, QualityPattern, SessionLocal,
    )
    db = SessionLocal()
    moment = datetime.now(timezone.utc)
    try:
        if not env_enabled("QUALITY_LEARNING_PROPOSALS_ENABLED"):
            raise RuntimeError("quality learning proposals are disabled")
        if not env_enabled("QUALITY_LEARNING_ABLATIONS_ENABLED"):
            raise RuntimeError("quality learning ablations are disabled")
        proposal = db.query(QualityFixProposal).filter(
            QualityFixProposal.id == proposal_id,
        ).with_for_update().one()
        pattern = db.query(QualityPattern).filter(
            QualityPattern.id == proposal.pattern_id,
        ).with_for_update().one()
        if pattern.status != "correlated":
            raise ValueError("source pattern is no longer qualified")
        experiment = db.query(QualityExperimentRun).filter(
            QualityExperimentRun.id == experiment_id,
            QualityExperimentRun.proposal_id == proposal_id,
        ).with_for_update().one()
        config = validate_proposal_config(proposal.candidate_config or {})
        candidate_hash = sha256_json(config)
        experiment.candidate_config_hash = candidate_hash
        experiment.status = "running"
        experiment.started_at = moment
        db.add(AuditLog(
            user_id=None,
            action="quality_learning.validation.started",
            detail={
                "proposal_id": proposal_id,
                "experiment_id": experiment_id,
                "candidate_config_sha256": candidate_hash,
            },
        ))
        db.commit()

        report_path = os.environ.get("QUALITY_LEARNING_BENCHMARK_REPORT_PATH", "").strip()
        if not report_path:
            raise ValueError("quality learning benchmark report is not configured")
        report, report_hash = _load_validation_report(Path(report_path))
        validation = _validate_report(report, proposal_id, candidate_hash)

        experiment = db.query(QualityExperimentRun).filter(
            QualityExperimentRun.id == experiment_id,
        ).with_for_update().one()
        proposal = db.query(QualityFixProposal).filter(
            QualityFixProposal.id == proposal_id,
        ).with_for_update().one()
        pattern = db.query(QualityPattern).filter(
            QualityPattern.id == proposal.pattern_id,
        ).with_for_update().one()
        if pattern.status != "correlated":
            raise ValueError("source pattern became stale during validation")
        experiment.status = "passed" if validation["passed"] else "failed"
        experiment.baseline_config_hash = validation["baseline_config_sha256"]
        experiment.benchmark_report_hash = report_hash
        experiment.metrics = validation
        experiment.completed_at = datetime.now(timezone.utc)
        proposal.status = "ready" if validation["passed"] else "failed"
        if validation["passed"]:
            pattern.status = "confirmed"
            pattern.updated_at = experiment.completed_at
            pattern.version = int(pattern.version or 0) + 1
        proposal.validation_summary = {
            **validation, "experiment_id": experiment.id,
            "benchmark_report_hash": report_hash,
        }
        proposal.updated_at = experiment.completed_at
        proposal.version = int(proposal.version or 0) + 1
        db.add(AuditLog(
            user_id=None,
            action="quality_learning.validation.completed",
            detail={
                "proposal_id": proposal_id,
                "experiment_id": experiment_id,
                "passed": bool(validation["passed"]),
                "benchmark_report_hash": report_hash,
            },
        ))
        db.commit()
        return {"proposal_id": proposal_id, **validation}
    except Exception as exc:
        db.rollback()
        experiment = db.query(QualityExperimentRun).filter(
            QualityExperimentRun.id == experiment_id,
        ).first()
        proposal = db.query(QualityFixProposal).filter(
            QualityFixProposal.id == proposal_id,
        ).first()
        if experiment:
            experiment.status = "blocked"
            experiment.failure_reason = str(exc)[:500]
            experiment.completed_at = datetime.now(timezone.utc)
        if proposal:
            proposal.status = "blocked"
            proposal.validation_summary = {"passed": False, "reason": str(exc)[:500]}
            proposal.updated_at = datetime.now(timezone.utc)
            proposal.version = int(proposal.version or 0) + 1
        db.add(AuditLog(
            user_id=None,
            action="quality_learning.validation.blocked",
            detail={
                "proposal_id": proposal_id,
                "experiment_id": experiment_id,
                "reason": str(exc)[:500],
            },
        ))
        db.commit()
        raise
    finally:
        db.close()
