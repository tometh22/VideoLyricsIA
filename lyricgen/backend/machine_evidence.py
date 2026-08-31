"""Durable, tenant-scoped evidence for the pre-human transcription state.

Analytics intentionally stores only hashes.  The editor, however, needs the
actual machine hypotheses to replay and learn from operator corrections.  This
module builds that private payload before the worker removes its internal ASR
streams and validates the immutable snapshot before a job becomes approvable.
"""
from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any


SCHEMA = "machine-transcription-evidence-v1"


class MachineSnapshotMissing(RuntimeError):
    """A job that requires machine evidence cannot enter approval."""


def _safe_json(value: Any) -> Any:
    """Clone provider payloads while making non-finite numbers JSON-safe."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _safe_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_json(item) for item in value]
    return str(value)


def snapshot_hash(value: Any) -> str:
    payload = json.dumps(
        _safe_json(value), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _provider_family(segments: list[dict]) -> str:
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        evidence = segment.get("provider_evidence") or {}
        family = str(evidence.get("correlated_family") or "").strip()
        if family:
            return family[:160]
        provenance = segment.get("content_provenance") or {}
        lineage = provenance.get("lineage") or {}
        family = str(lineage.get("correlated_family") or "").strip()
        if family:
            return family[:160]
    return "unknown-primary-asr"


def build_machine_evidence(result: dict) -> dict:
    """Capture every available recognition family before private keys vanish.

    The selected output is always present as a family.  Raw primary,
    independent and pre-anchor streams are added when the pipeline produced
    them, so a future replay can distinguish generation from ranking errors.
    """
    if not isinstance(result, dict):
        raise ValueError("machine evidence requires a transcription result")
    selected = [row for row in (result.get("segments") or []) if isinstance(row, dict)]
    hypotheses: list[dict] = []

    def add(role: str, family: str, kind: str, events: Any) -> None:
        rows = [row for row in (events or []) if isinstance(row, dict)]
        if not rows:
            return
        hypotheses.append({
            "role": role,
            "family": str(family or "unknown")[:160],
            "kind": kind,
            "events": _safe_json(rows),
            "event_count": len(rows),
            "events_sha256": snapshot_hash(rows),
        })

    primary_family = str(
        result.get("_primary_asr_family") or _provider_family(selected)
    )
    add("primary", primary_family, "word_stream", result.get("_asr_words"))
    add(
        "independent",
        str(result.get("_independent_asr_family") or "independent-asr"),
        "word_stream",
        result.get("_independent_asr_words"),
    )
    add(
        "pre_anchor",
        str(result.get("_provider_asr_family") or primary_family),
        "segments",
        result.get("_pre_anchor_provider_segments"),
    )
    add("selected", primary_family, "segments", selected)
    if not hypotheses:
        # An empty transcription is still a pre-human state that must be
        # auditable.  Persist an explicit empty selected family, never a null.
        hypotheses.append({
            "role": "selected", "family": primary_family,
            "kind": "segments", "events": [], "event_count": 0,
            "events_sha256": snapshot_hash([]),
        })

    return {
        "schema": SCHEMA,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "hypotheses_by_family": hypotheses,
        "capture": {
            "primary_present": bool(result.get("_asr_words")),
            "independent_present": bool(result.get("_independent_asr_words")),
            "pre_anchor_present": bool(result.get("_pre_anchor_provider_segments")),
        },
    }


def finalize_machine_evidence(
    evidence: dict,
    *,
    original_segments: list[dict],
    quality: dict | None,
    audio_sha256: str | None,
    audio_revision: int,
) -> dict:
    """Bind captured hypotheses to the exact durable editor snapshot."""
    if not isinstance(evidence, dict) or evidence.get("schema") != SCHEMA:
        raise ValueError("machine evidence was not captured before persistence")
    payload = deepcopy(_safe_json(evidence))
    payload["pre_human"] = {
        "segments_sha256": snapshot_hash(original_segments or []),
        "segment_count": len(original_segments or []),
        "audio_sha256": str(audio_sha256 or "")[:64] or None,
        "audio_revision": max(0, int(audio_revision or 0)),
    }
    quality_payload = dict(quality or {})
    payload["decisions"] = {
        "quality": _safe_json(quality_payload),
        "route": str(
            quality_payload.get("route")
            or quality_payload.get("decision") or "unknown"
        )[:64],
        "policy_version": str(quality_payload.get("policy_version") or "unknown")[:64],
        "timing_source": str(quality_payload.get("timing_source") or "unknown")[:64],
    }
    payload["evidence_sha256"] = snapshot_hash({
        "hypotheses_by_family": payload["hypotheses_by_family"],
        "pre_human": payload["pre_human"],
        "decisions": payload["decisions"],
    })
    return payload


def validate_machine_evidence(evidence: Any, original_segments: Any) -> None:
    if not isinstance(evidence, dict) or evidence.get("schema") != SCHEMA:
        raise MachineSnapshotMissing("machine_snapshot_missing")
    hypotheses = evidence.get("hypotheses_by_family")
    if not isinstance(hypotheses, list) or not hypotheses:
        raise MachineSnapshotMissing("machine_hypotheses_missing")
    if not isinstance(evidence.get("decisions"), dict):
        raise MachineSnapshotMissing("machine_decisions_missing")
    pre_human = evidence.get("pre_human") or {}
    if pre_human.get("segments_sha256") != snapshot_hash(original_segments or []):
        raise MachineSnapshotMissing("machine_snapshot_hash_mismatch")

