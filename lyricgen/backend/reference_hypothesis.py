"""Durable, audio-bound lyric reference hypotheses for batch review.

References are never treated as ground truth merely because a provider
returned them.  The batch workflow binds the candidate to the exact uploaded
audio and requires a human to verify every current editor line before render.
"""

from __future__ import annotations

import hashlib
from typing import Any


SCHEMA = "batch-reference-hypothesis-v1"
_RECOVERABLE_EVIDENCE_VIEW = "full_audio_with_reference"
_RECOVERABLE_EVIDENCE_TRANSFORMATIONS = {
    "gemini_cleanup_raw",
    "gemini_reference_hypothesis_raw",
}


def audio_only_batch_mode(*, reference_required: bool, workload_class: str) -> bool:
    """Return whether this transcription must avoid every external lyric source.

    Batch references are produced only by listening to the complete uploaded
    audio.  External URLs may be displayed to a human reviewer, but their text
    is never fetched, cached, aligned or supplied to the transcription engine.
    """
    return bool(reference_required and str(workload_class or "").lower() == "batch")


def text_sha256(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def build(
    *,
    text: str,
    provider: str,
    audio_sha256: str,
    audio_revision: int,
    source_kind: str,
    complete_audio_verified: bool,
    attestation: dict[str, Any] | None = None,
    source_version: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized = "\n".join(line.strip() for line in (text or "").splitlines() if line.strip())
    return {
        "schema": SCHEMA,
        "audio_sha256": str(audio_sha256 or ""),
        "audio_revision": int(audio_revision or 0),
        "source": {
            "kind": source_kind,
            "provider": str(provider or "unknown"),
            "version": dict(source_version or {}),
        },
        "reference_text": normalized,
        "reference_sha256": text_sha256(normalized),
        "line_count": len(normalized.splitlines()) if normalized else 0,
        "verification": {
            "complete_audio": bool(complete_audio_verified),
            "memory_completion_prohibited": True,
            "unconfirmed_text_allowed": False,
        },
        "attestation": dict(attestation or {}),
        "confidence": {
            "method": "independent_audio_attestation",
            "status": str((attestation or {}).get("text_status") or "pending"),
            "score": ((attestation or {}).get("metrics") or {}).get("attestation_score"),
        },
        "review_status": "pending_human_line_review",
    }


def build_unavailable(
    *,
    audio_sha256: str,
    audio_revision: int,
    provider: str = "gemini-2.5-flash-audio",
    attestation: dict[str, Any] | None = None,
    source_version: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Bind a failed full-audio hypothesis attempt without blocking ASR.

    This is intentionally not a lyric reference: it carries no text and may
    never be applied automatically.  It exists so the review queue can mark
    the exact recording red and require a complete manual line review while
    preserving the same audio/revision gate used by normal hypotheses.
    """
    return {
        "schema": SCHEMA,
        "availability": "unavailable",
        "audio_sha256": str(audio_sha256 or ""),
        "audio_revision": int(audio_revision or 0),
        "source": {
            "kind": "gemini_complete_audio_hypothesis_unavailable",
            "provider": str(provider or "gemini-2.5-flash-audio"),
            "version": dict(source_version or {}),
        },
        "reference_text": "",
        "reference_sha256": text_sha256(""),
        "line_count": 0,
        "verification": {
            "complete_audio": True,
            "memory_completion_prohibited": True,
            "unconfirmed_text_allowed": False,
        },
        "attestation": {
            **dict(attestation or {}),
            "text_status": "manual_full_review_required",
            "reason": "full_audio_hypothesis_unavailable",
        },
        "confidence": {
            "method": "complete_audio_attempt",
            "status": "manual_full_review_required",
            "score": None,
        },
        "review_status": "manual_full_review_required",
    }


def build_from_candidate(
    candidate: dict[str, Any] | None,
    *,
    fallback_text: str,
    audio_sha256: str,
    audio_revision: int,
) -> tuple[dict[str, Any], bool]:
    """Build the durable reference marker and return whether review is manual.

    A missing Gemini result deliberately returns an unavailable marker instead
    of raising, so transcription can finish and enter the red review queue.
    """
    candidate = candidate if isinstance(candidate, dict) else {}
    text = str(candidate.get("text") or fallback_text or "").strip()
    common = {
        "provider": str(candidate.get("provider") or "gemini-2.5-flash-audio"),
        "audio_sha256": audio_sha256,
        "audio_revision": audio_revision,
        "attestation": candidate.get("attestation") or {},
        "source_version": candidate.get("source_version") or {},
    }
    if not text:
        return build_unavailable(**common), True
    return build(
        text=text,
        source_kind=str(candidate.get("source_kind") or "unknown"),
        complete_audio_verified=bool(candidate.get("complete_audio_verified")),
        **common,
    ), False


def recover_from_machine_evidence(
    evidence: Any,
    *,
    original_segments: list[dict[str, Any]],
    audio_sha256: str,
    audio_revision: int,
) -> tuple[dict[str, Any] | None, str]:
    """Recover a missing reference from an immutable full-audio snapshot.

    A historical quality-replay race could replace ``transcription_quality``
    after the worker attached ``reference_hypothesis``. Recovery is deliberately
    narrow: every machine-evidence hash, the selected pre-human snapshot and the
    exact audio identity must validate. Edited lyrics, catalogues, memory and
    partial ASR streams are never accepted as substitutes.
    """
    from machine_evidence import (
        MachineSnapshotMissing,
        SCHEMA as MACHINE_EVIDENCE_SCHEMA,
        validate_machine_evidence,
    )

    if not isinstance(evidence, dict) or evidence.get("schema") != MACHINE_EVIDENCE_SCHEMA:
        return None, "reference_recovery_machine_evidence_missing"
    if (
        not isinstance(audio_sha256, str)
        or len(audio_sha256) != 64
        or any(character not in "0123456789abcdef" for character in audio_sha256)
    ):
        return None, "reference_recovery_audio_identity_missing"
    try:
        validate_machine_evidence(evidence, original_segments)
    except MachineSnapshotMissing as exc:
        return None, str(exc) or "reference_recovery_machine_evidence_invalid"

    pre_human = evidence.get("pre_human") or {}
    if str(pre_human.get("audio_sha256") or "") != audio_sha256:
        return None, "reference_recovery_audio_mismatch"
    if int(pre_human.get("audio_revision") or 0) != int(audio_revision or 0):
        return None, "reference_recovery_audio_revision_mismatch"

    candidates = [
        candidate
        for candidate in (evidence.get("hypotheses_by_family") or [])
        if isinstance(candidate, dict)
        and candidate.get("role") == "primary"
        and candidate.get("kind") == "text"
        and candidate.get("view") == _RECOVERABLE_EVIDENCE_VIEW
        and candidate.get("transformation")
        in _RECOVERABLE_EVIDENCE_TRANSFORMATIONS
        and "gemini" in str(candidate.get("family") or "").casefold()
    ]
    if len(candidates) != 1:
        return None, "reference_recovery_candidate_missing"
    events = candidates[0].get("events") or []
    if len(events) != 1 or not isinstance(events[0], dict):
        return None, "reference_recovery_candidate_invalid"
    text = str(events[0].get("text") or "").strip()
    if not text:
        return None, "reference_recovery_candidate_empty"

    quality = (evidence.get("decisions") or {}).get("quality") or {}
    hypothesis = build(
        text=text,
        provider=str(candidates[0].get("family") or "gemini-2.5-flash-audio"),
        audio_sha256=audio_sha256,
        audio_revision=audio_revision,
        source_kind="gemini_complete_audio_derived",
        complete_audio_verified=True,
        attestation=(
            quality.get("reference_attestation")
            if isinstance(quality, dict) else {}
        ) or {},
        source_version={
            "pipeline_release": (
                quality.get("pipeline_release")
                if isinstance(quality, dict) else None
            ),
            "pipeline_config_fingerprint": (
                quality.get("pipeline_config_fingerprint")
                if isinstance(quality, dict) else None
            ),
            "recovered_from": "machine_evidence",
        },
    )
    valid, reason = validate_binding(
        hypothesis,
        audio_sha256=audio_sha256,
        audio_revision=audio_revision,
    )
    return (hypothesis, "ok") if valid else (None, reason)


def validate_binding(
    hypothesis: Any,
    *,
    audio_sha256: str,
    audio_revision: int,
) -> tuple[bool, str]:
    if not isinstance(hypothesis, dict) or hypothesis.get("schema") != SCHEMA:
        return False, "reference_hypothesis_missing"
    text = str(hypothesis.get("reference_text") or "")
    unavailable = hypothesis.get("availability") == "unavailable"
    if unavailable:
        if text or hypothesis.get("reference_sha256") != text_sha256(""):
            return False, "reference_hypothesis_invalid"
    elif not text or hypothesis.get("reference_sha256") != text_sha256(text):
        return False, "reference_hypothesis_invalid"
    if str(hypothesis.get("audio_sha256") or "") != str(audio_sha256 or ""):
        return False, "reference_audio_mismatch"
    if int(hypothesis.get("audio_revision") or 0) != int(audio_revision or 0):
        return False, "reference_audio_revision_mismatch"
    verification = hypothesis.get("verification") or {}
    if verification.get("complete_audio") is not True:
        return False, "reference_complete_audio_unverified"
    if verification.get("memory_completion_prohibited") is not True:
        return False, "reference_memory_policy_missing"
    return True, "ok"
