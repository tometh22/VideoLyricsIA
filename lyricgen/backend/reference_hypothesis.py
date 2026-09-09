"""Durable, audio-bound lyric reference hypotheses for batch review.

References are never treated as ground truth merely because a provider
returned them.  The batch workflow binds the candidate to the exact uploaded
audio and requires a human to verify every current editor line before render.
"""

from __future__ import annotations

import hashlib
from typing import Any


SCHEMA = "batch-reference-hypothesis-v1"


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
