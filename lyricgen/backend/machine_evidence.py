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


SCHEMA = "machine-transcription-evidence-v3"
LEGACY_SCHEMAS = {
    "machine-transcription-evidence-v1",
    "machine-transcription-evidence-v2",
}


class MachineSnapshotMissing(RuntimeError):
    """A job that requires machine evidence cannot enter approval."""


def _safe_json(value: Any) -> Any:
    """Clone payloads into the canonical representation stored by JSONB."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        # PostgreSQL JSONB normalizes IEEE negative zero to positive zero.
        # Hash that same canonical representation so a database round-trip
        # cannot invalidate otherwise immutable evidence.
        return 0.0 if value == 0.0 else value
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


def quality_training_signal(quality: dict | None) -> dict:
    """Freeze the song-level traffic light used by calibration.

    Quality v6 deliberately leaves ``score`` null while calibration is in
    observe mode.  The underlying risk is still a bounded song-level signal,
    so persist both the raw score and an explicit derived score.  Consumers
    can distinguish them through ``score_source`` instead of silently treating
    a missing score as zero.
    """
    payload = dict(quality) if isinstance(quality, dict) else {}
    decision = str(payload.get("decision") or payload.get("verdict") or "unknown")[:32]
    raw_score = payload.get("score")
    risk = payload.get("risk")

    score = None
    score_source = "unavailable"
    if isinstance(raw_score, (int, float)) and not isinstance(raw_score, bool):
        value = float(raw_score)
        if math.isfinite(value):
            score = round(max(0.0, min(100.0, value)), 3)
            score_source = "quality_score"
    if score is None and isinstance(risk, (int, float)) and not isinstance(risk, bool):
        value = float(risk)
        if math.isfinite(value):
            risk = round(max(0.0, min(1.0, value)), 6)
            score = round(100.0 * (1.0 - risk), 3)
            score_source = "risk_derived"
        else:
            risk = None
    elif not isinstance(risk, (int, float)) or isinstance(risk, bool):
        risk = None
    else:
        risk = round(max(0.0, min(1.0, float(risk))), 6)

    hard_red = decision in {
        "unsafe", "fail", "failed", "blocked", "retry_failed",
    }
    if hard_red or (risk is not None and risk >= 0.75):
        traffic_light = "red"
    elif decision in {"pass", "approved", "safe"} and (
        score is None or score >= 90.0
    ):
        traffic_light = "green"
    else:
        traffic_light = "yellow"
    return {
        "schema": "song-quality-signal-v1",
        "traffic_light": traffic_light,
        "verdict": decision,
        "score": score,
        "score_source": score_source,
        "raw_score": (
            round(float(raw_score), 3)
            if isinstance(raw_score, (int, float))
            and not isinstance(raw_score, bool)
            and math.isfinite(float(raw_score))
            else None
        ),
        "risk": risk,
        "policy_version": str(payload.get("policy_version") or "unknown")[:64],
    }


def validate_quality_training_signal(signal: Any) -> None:
    """Validate the persisted song-level calibration label without inventing it."""
    if not isinstance(signal, dict) or signal.get("schema") != "song-quality-signal-v1":
        raise MachineSnapshotMissing("machine_quality_signal_missing")
    traffic_light = signal.get("traffic_light")
    if not isinstance(traffic_light, str) or traffic_light not in {
        "green", "yellow", "red",
    }:
        raise MachineSnapshotMissing("machine_quality_signal_invalid")
    score_source = signal.get("score_source")
    if not isinstance(score_source, str) or score_source not in {
        "quality_score", "risk_derived", "unavailable",
    }:
        raise MachineSnapshotMissing("machine_quality_score_source_invalid")
    if score_source == "quality_score":
        reconstruction = {
            "decision": signal.get("verdict"),
            "score": signal.get("raw_score"),
            "risk": signal.get("risk"),
            "policy_version": signal.get("policy_version"),
        }
    elif score_source == "risk_derived":
        reconstruction = {
            "decision": signal.get("verdict"),
            "risk": signal.get("risk"),
            "policy_version": signal.get("policy_version"),
        }
    else:
        reconstruction = {
            "decision": signal.get("verdict"),
            "policy_version": signal.get("policy_version"),
        }
    if quality_training_signal(reconstruction) != signal:
        raise MachineSnapshotMissing("machine_quality_signal_inconsistent")


def _provider_family(segments: list[dict]) -> str:
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        evidence = segment.get("provider_evidence") or {}
        family = str(evidence.get("correlated_family") or "").strip()
        if family and family.casefold() not in {"unknown", "unknown-primary-asr"}:
            return family[:160]
        provenance = segment.get("content_provenance") or {}
        lineage = provenance.get("lineage") or {}
        family = str(lineage.get("correlated_family") or "").strip()
        if family and family.casefold() not in {"unknown", "unknown-primary-asr"}:
            return family[:160]
        source = str(
            segment.get("content_source")
            or evidence.get("source")
            or ""
        ).strip()
        if source:
            return source[:160]
    return "selected-output"


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

    def add(
        role: str, family: str, kind: str, events: Any,
        *, allow_empty: bool = False, **metadata: Any,
    ) -> None:
        rows = [row for row in (events or []) if isinstance(row, dict)]
        if not rows and not allow_empty:
            return
        hypothesis = {
            "role": role,
            "family": str(family or "unknown")[:160],
            "kind": kind,
            "events": _safe_json(rows),
            "event_count": len(rows),
            "events_sha256": snapshot_hash(rows),
        }
        for key in ("view", "transformation"):
            if metadata.get(key):
                hypothesis[key] = str(metadata[key])[:160]
        if type(metadata.get("attempt_id")) is int:
            hypothesis["attempt_id"] = metadata["attempt_id"]
        hypotheses.append(hypothesis)

    recognition_attempts = [
        item for item in (result.get("_recognition_hypotheses") or [])
        if isinstance(item, dict)
    ]
    for index, attempt in enumerate(recognition_attempts):
        add(
            "primary" if index == 0 else "candidate",
            str(attempt.get("family") or ""),
            str(attempt.get("kind") or "segments"),
            attempt.get("events"),
            allow_empty=True,
            attempt_id=(
                attempt.get("attempt_id")
                if type(attempt.get("attempt_id")) is int else index
            ),
            view=attempt.get("view"),
            transformation=attempt.get("transformation"),
        )

    primary_family = str(
        result.get("_primary_asr_family") or _provider_family(selected)
    )
    if not recognition_attempts:
        add(
            "primary", primary_family, "word_stream",
            result.get("_asr_words"), attempt_id=0,
        )
    add(
        "independent",
        str(result.get("_independent_asr_family") or "independent-asr"),
        "word_stream",
        result.get("_independent_asr_words"),
    )
    pre_anchor_segments = [
        row for row in (result.get("_pre_anchor_provider_segments") or [])
        if isinstance(row, dict)
    ]
    add(
        "pre_anchor",
        str(
            result.get("_provider_asr_family")
            or _provider_family(pre_anchor_segments)
        ),
        "segments",
        pre_anchor_segments,
    )
    selected_family = _provider_family(selected)
    add("selected", selected_family, "segments", selected)
    if not any(item.get("role") == "selected" for item in hypotheses):
        # An empty selected transcription is still the pre-human state even
        # when a raw word family exists. Persist it explicitly, never infer it
        # from another family's events.
        hypotheses.append({
            "role": "selected", "family": selected_family,
            "kind": "segments", "events": [], "event_count": 0,
            "events_sha256": snapshot_hash([]),
        })

    inferred_attempt_count = sum(
        item.get("role") in {"primary", "candidate"}
        for item in hypotheses
    )
    reported_attempt_count = result.get("_recognition_attempt_count")
    if type(reported_attempt_count) is not int:
        reported_attempt_count = inferred_attempt_count
    return {
        "schema": SCHEMA,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "hypotheses_by_family": hypotheses,
        "capture": {
            "recognition_attempt_count": reported_attempt_count,
            "primary_present": bool(
                recognition_attempts or result.get("_asr_words")
            ),
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
    canonical_selected = _safe_json(original_segments or [])
    selected_found = False
    for hypothesis in payload.get("hypotheses_by_family") or []:
        if isinstance(hypothesis, dict) and hypothesis.get("role") == "selected":
            hypothesis["kind"] = "segments"
            hypothesis["events"] = canonical_selected
            hypothesis["event_count"] = len(canonical_selected)
            hypothesis["events_sha256"] = snapshot_hash(canonical_selected)
            selected_found = True
    if not selected_found:
        payload.setdefault("hypotheses_by_family", []).append({
            "role": "selected",
            "family": "selected-output",
            "kind": "segments",
            "events": canonical_selected,
            "event_count": len(canonical_selected),
            "events_sha256": snapshot_hash(canonical_selected),
        })
    payload["pre_human"] = {
        "segments_sha256": snapshot_hash(canonical_selected),
        "segment_count": len(canonical_selected),
        "audio_sha256": str(audio_sha256 or "")[:64] or None,
        "audio_revision": max(0, int(audio_revision or 0)),
    }
    quality_payload = dict(quality) if isinstance(quality, dict) else {}
    payload["decisions"] = {
        "quality": _safe_json(quality_payload),
        "song_quality_signal": quality_training_signal(quality_payload),
        "route": str(
            quality_payload.get("route")
            or quality_payload.get("decision") or "unknown"
        )[:64],
        "policy_version": str(quality_payload.get("policy_version") or "unknown")[:64],
        "timing_source": str(quality_payload.get("timing_source") or "unknown")[:64],
    }
    payload["evidence_sha256"] = snapshot_hash({
        "hypotheses_by_family": payload["hypotheses_by_family"],
        "capture": payload.get("capture") or {},
        "pre_human": payload["pre_human"],
        "decisions": payload["decisions"],
    })
    return payload


def validate_machine_evidence(evidence: Any, original_segments: Any) -> None:
    if not isinstance(evidence, dict) or evidence.get("schema") not in {
        SCHEMA, *LEGACY_SCHEMAS,
    }:
        raise MachineSnapshotMissing("machine_snapshot_missing")
    hypotheses = evidence.get("hypotheses_by_family")
    if not isinstance(hypotheses, list) or not hypotheses:
        raise MachineSnapshotMissing("machine_hypotheses_missing")
    if not isinstance(evidence.get("decisions"), dict):
        raise MachineSnapshotMissing("machine_decisions_missing")
    pre_human = evidence.get("pre_human")
    if not isinstance(pre_human, dict):
        raise MachineSnapshotMissing("machine_pre_human_missing")
    if pre_human.get("segments_sha256") != snapshot_hash(original_segments or []):
        raise MachineSnapshotMissing("machine_snapshot_hash_mismatch")
    if evidence.get("schema") == SCHEMA:
        selected_matches_snapshot = False
        named_primary_hypotheses = 0
        primary_attempt_ids: list[Any] = []
        for hypothesis in hypotheses:
            if not isinstance(hypothesis, dict):
                raise MachineSnapshotMissing("machine_hypothesis_invalid")
            role = hypothesis.get("role")
            family = hypothesis.get("family")
            kind = hypothesis.get("kind")
            events = hypothesis.get("events")
            event_count = hypothesis.get("event_count")
            event_hash = hypothesis.get("events_sha256")
            if (
                not isinstance(role, str) or not role.strip()
                or not isinstance(family, str) or not family.strip()
                or not isinstance(kind, str) or not kind.strip()
                or not isinstance(events, list)
                or any(not isinstance(event, dict) for event in events)
                or type(event_count) is not int
                or event_count != len(events)
                or not isinstance(event_hash, str)
                or event_hash != snapshot_hash(events)
            ):
                raise MachineSnapshotMissing("machine_hypothesis_invalid")
            if family.casefold() in {
                "unknown", "unknown-primary-asr", "independent-asr",
            }:
                raise MachineSnapshotMissing("machine_hypothesis_family_unknown")
            if role in {"primary", "candidate"}:
                named_primary_hypotheses += 1
                attempt_id = hypothesis.get("attempt_id")
                primary_attempt_ids.append(attempt_id)
            if role == "selected":
                if kind != "segments":
                    raise MachineSnapshotMissing("machine_selected_hypothesis_invalid")
                if event_hash == pre_human.get("segments_sha256"):
                    selected_matches_snapshot = True
        if not selected_matches_snapshot:
            raise MachineSnapshotMissing("machine_selected_hypothesis_missing")
        capture = evidence.get("capture")
        if not isinstance(capture, dict):
            raise MachineSnapshotMissing("machine_capture_summary_missing")
        attempt_count = capture.get("recognition_attempt_count")
        if type(attempt_count) is not int or attempt_count < 0:
            raise MachineSnapshotMissing("machine_recognition_attempt_count_invalid")
        if attempt_count != named_primary_hypotheses:
            raise MachineSnapshotMissing("machine_recognition_hypothesis_missing")
        if any(
            type(attempt_id) is not int or attempt_id < 0
            for attempt_id in primary_attempt_ids
        ):
            raise MachineSnapshotMissing("machine_recognition_attempt_id_invalid")
        if sorted(primary_attempt_ids) != list(range(attempt_count)):
            raise MachineSnapshotMissing("machine_recognition_attempt_id_invalid")
        signal = (evidence.get("decisions") or {}).get("song_quality_signal")
        validate_quality_training_signal(signal)
        expected_hash = snapshot_hash({
            "hypotheses_by_family": evidence["hypotheses_by_family"],
            "capture": evidence["capture"],
            "pre_human": evidence["pre_human"],
            "decisions": evidence["decisions"],
        })
        if evidence.get("evidence_sha256") != expected_hash:
            raise MachineSnapshotMissing("machine_evidence_hash_mismatch")


def approval_training_provenance(
    *, segments: list[dict], quality: dict | None, revision: int,
) -> dict:
    """Immutable label-side evidence attached to an approved editor version."""
    payload = {
        "schema": "training-approval-evidence-v1",
        "revision": int(revision),
        "segments_sha256": snapshot_hash(segments or []),
        "song_quality_signal": quality_training_signal(quality),
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }
    payload["evidence_sha256"] = snapshot_hash({
        key: value for key, value in payload.items() if key != "evidence_sha256"
    })
    return payload
