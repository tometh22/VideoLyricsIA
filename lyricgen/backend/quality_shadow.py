"""Privacy-safe, append-only evidence for transcription shadow rollout.

The event deliberately stores hashes and counters, never lyric text or audio.
It can later be joined to ``editor_approved`` by job/revision and exported to
the attested benchmark ledger after an independent reviewer labels correctness.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any


EVENT_NAME = "transcription_quality_shadow_decision"


def decision_identity(job_id: str, revision: int, segments_hash: str,
                      pipeline_release: str,
                      pipeline_config_fingerprint: str) -> dict:
    """Canonical rollout unit: one immutable lyric snapshot per candidate."""
    return {
        "job_id": str(job_id or ""), "revision": int(revision or 0),
        "segments_hash": str(segments_hash or ""),
        "pipeline_release": str(pipeline_release or "unknown")[:64],
        "pipeline_config_fingerprint": str(
            pipeline_config_fingerprint or "unknown"
        )[:32],
    }


def decision_id(identity: dict) -> str:
    canonical = json.dumps(
        identity, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def build_shadow_event(job: Any, quality: dict, *,
                       evaluation_stage: str = "terminal",
                       occurred_at: datetime | None = None) -> dict:
    shadow = quality.get("shadow_decision") or {}
    identity = decision_identity(
        str(getattr(job, "job_id", "") or ""),
        int(quality.get("evaluated_revision") or 0),
        str(quality.get("segments_hash") or ""),
        str(quality.get("pipeline_release") or "unknown"),
        str(quality.get("pipeline_config_fingerprint") or "unknown"),
    )
    moment = occurred_at or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    properties = {
        **identity,
        "decision_id": decision_id(identity),
        "quality_fingerprint": str(quality.get("quality_fingerprint") or ""),
        "policy_version": str(quality.get("policy_version") or "unknown")[:64],
        "evaluation_stage": (
            evaluation_stage if evaluation_stage in {"initial", "terminal"}
            else "initial"
        ),
        "eligible": bool(shadow.get("eligible")),
        "would_approve": bool(shadow.get("would_approve")),
        "machine_decision": str(quality.get("decision") or "unknown")[:32],
        "reason_codes": [
            str(value)[:80] for value in (shadow.get("reason_codes") or [])[:32]
        ],
        "timing_source": str(quality.get("timing_source") or "unknown")[:64],
        "api_cost_usd": (quality.get("quality_job") or {}).get("api_cost_usd"),
        "cost_complete": bool(
            (quality.get("quality_job") or {}).get("cost_complete", False)
        ),
    }
    return {"occurred_at": moment, "properties": properties}


def record_shadow_decision(db: Any, job: Any, quality: dict,
                           *, previous_quality: dict | None = None,
                           evaluation_stage: str = "terminal") -> bool:
    """Append one machine decision unless this exact fingerprint was stored."""
    if not isinstance(quality, dict) or not isinstance(
        quality.get("shadow_decision"), dict,
    ):
        return False
    if (
        evaluation_stage != "terminal"
        and
        isinstance(previous_quality, dict)
        and previous_quality.get("quality_fingerprint")
        and previous_quality.get("quality_fingerprint")
        == quality.get("quality_fingerprint")
    ):
        return False
    from database import ProductEvent

    event = build_shadow_event(
        job, quality, evaluation_stage=evaluation_stage,
    )
    db.add(ProductEvent(
        tenant_id=str(getattr(job, "tenant_id", "") or "unknown")[:100],
        user_id=getattr(job, "user_id", None),
        job_id=str(getattr(job, "job_id", "") or "")[:12] or None,
        name=EVENT_NAME,
        occurred_at=event["occurred_at"],
        properties=event["properties"],
    ))
    return True
