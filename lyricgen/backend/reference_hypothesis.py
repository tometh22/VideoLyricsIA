"""Durable, audio-bound lyric reference hypotheses for batch review.

References are never treated as ground truth merely because a provider
returned them.  The batch workflow binds the candidate to the exact uploaded
audio and requires a human to verify every current editor line before render.
"""

from __future__ import annotations

import hashlib
from typing import Any


SCHEMA = "batch-reference-hypothesis-v1"


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


def validate_binding(
    hypothesis: Any,
    *,
    audio_sha256: str,
    audio_revision: int,
) -> tuple[bool, str]:
    if not isinstance(hypothesis, dict) or hypothesis.get("schema") != SCHEMA:
        return False, "reference_hypothesis_missing"
    text = str(hypothesis.get("reference_text") or "")
    if not text or hypothesis.get("reference_sha256") != text_sha256(text):
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
