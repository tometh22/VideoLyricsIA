"""Attest catalogue lyrics against an audio-first ASR transcript.

Catalogue text is a candidate.  This module decides whether the independent
audio-first word sequence supports using that candidate for local vocabulary
correction or whole-song alignment.  It emits metrics only, never lyric text.
"""
from __future__ import annotations

from collections import Counter
from difflib import SequenceMatcher
import math
import re
import unicodedata
from typing import Any, Mapping, Sequence

from evidence_contracts import privacy_fingerprint


_TRUSTED_SOURCES = {
    "human_verified", "user_verified", "accepted_delivery", "licensed_provider",
}
_STOP = {
    "a", "al", "de", "del", "el", "en", "es", "la", "las", "lo", "los",
    "o", "por", "que", "se", "si", "su", "un", "una", "y",
    "a", "an", "and", "are", "at", "for", "in", "is", "it", "of", "on",
    "the", "to", "you",
}


def _tokens(value: Any) -> list[str]:
    normalized = unicodedata.normalize("NFKD", str(value or "").casefold())
    normalized = "".join(
        char for char in normalized if unicodedata.category(char) != "Mn"
    )
    return [
        token for token in re.findall(r"[^\W_]+", normalized, re.UNICODE)
        if token not in _STOP
    ]


def _segments_text(segments: Sequence[Mapping[str, Any]]) -> str:
    return " ".join(
        str(segment.get("text") or segment.get("t") or "")
        for segment in segments if isinstance(segment, Mapping)
    )


def _multiset_overlap(left: list[str], right: list[str]) -> int:
    return sum((Counter(left) & Counter(right)).values())


def _finite(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def assess_reference_attestation(
    reference_text: str,
    asr_segments: Sequence[Mapping[str, Any]],
    *,
    reference_source: str = "catalog_unverified",
    audio_duration_s: float | None = None,
    is_live: bool = False,
) -> dict[str, Any]:
    reference_tokens = _tokens(reference_text)
    asr_tokens = _tokens(_segments_text(asr_segments))
    overlap = _multiset_overlap(reference_tokens, asr_tokens)
    asr_containment = overlap / len(asr_tokens) if asr_tokens else 0.0
    reference_coverage = overlap / len(reference_tokens) if reference_tokens else 0.0
    ordered_similarity = SequenceMatcher(None, reference_tokens, asr_tokens).ratio()
    attestation_score = (
        0.45 * ordered_similarity
        + 0.35 * asr_containment
        + 0.20 * reference_coverage
    )

    duration = _finite(audio_duration_s)
    ends = [
        value for value in (
            _finite(segment.get("end", segment.get("e")))
            for segment in asr_segments if isinstance(segment, Mapping)
        ) if value is not None
    ]
    last_end = max(ends, default=0.0)
    trailing_gap = max(0.0, duration - last_end) if duration else None
    timeline_observed = bool(
        duration is None
        or trailing_gap is not None
        and trailing_gap <= max(45.0, 0.20 * duration)
    )

    trusted = str(reference_source or "") in _TRUSTED_SOURCES
    independently_attested = bool(
        len(reference_tokens) >= 4
        and len(asr_tokens) >= 4
        and attestation_score >= 0.62
        and asr_containment >= 0.62
        and reference_coverage >= 0.50
    )
    local_vocabulary_supported = bool(
        trusted
        or independently_attested
        or (
            attestation_score >= 0.56
            and asr_containment >= 0.65
            and reference_coverage >= 0.35
        )
    )
    global_alignment_supported = bool(
        local_vocabulary_supported
        and not is_live
        and timeline_observed
        and reference_coverage >= 0.60
        and ordered_similarity >= 0.52
    )
    if trusted:
        status = "trusted"
    elif independently_attested:
        status = "independently_attested"
    elif local_vocabulary_supported:
        status = "local_vocabulary_only"
    else:
        status = "unsafe_without_witness"
    reasons = []
    if not local_vocabulary_supported:
        reasons.append("reference_text_not_attested")
    if not timeline_observed:
        reasons.append("asr_timeline_incomplete")
    if is_live:
        reasons.append("live_structure_requires_local_alignment")
    if local_vocabulary_supported and not global_alignment_supported and not is_live:
        reasons.append("global_structure_not_attested")

    return {
        "schema_version": "reference-asr-attestation-v1",
        "reference_source": reference_source,
        "text_status": status,
        "allow_vocabulary_reconciliation": local_vocabulary_supported,
        "allow_global_forced_alignment": global_alignment_supported,
        "require_local_alignment": not global_alignment_supported,
        "reasons": reasons,
        "metrics": {
            "reference_token_count": len(reference_tokens),
            "asr_token_count": len(asr_tokens),
            "ordered_similarity": round(ordered_similarity, 6),
            "asr_token_containment": round(asr_containment, 6),
            "reference_token_coverage": round(reference_coverage, 6),
            "attestation_score": round(attestation_score, 6),
            "timeline_observed": timeline_observed,
            "trailing_gap_s": round(trailing_gap, 6) if trailing_gap is not None else None,
        },
        "identities": {
            # Catalogue lyrics are low-entropy copyrighted text.  Plain
            # SHA-256 would be dictionary-reversible, so identities abstain
            # unless the deployment has a strong HMAC key configured.
            "reference_fingerprint": privacy_fingerprint(
                "reference-attestation-candidate", reference_text,
            ),
            "asr_fingerprint": privacy_fingerprint(
                "reference-attestation-asr", _segments_text(asr_segments),
            ),
        },
    }


def reference_gate_action(
    report: Mapping[str, Any] | None,
    *,
    mode: str,
    is_live: bool,
) -> str:
    """Choose the mutation policy without conflating text and structure.

    ``observe`` never changes output.  In ``enforce`` a studio catalogue may
    only own whole-song reconciliation when both its vocabulary and structure
    were independently attested.  Live recordings may use an attested
    catalogue locally, but their performance order always remains audio-owned.
    """
    normalized_mode = str(mode or "off").strip().lower()
    if normalized_mode not in {"observe", "enforce"} or not report:
        return "disabled"
    if normalized_mode == "observe":
        return "observe"
    if not bool(report.get("allow_vocabulary_reconciliation")):
        return "audio_first"
    if is_live:
        return "local_only"
    if not bool(report.get("allow_global_forced_alignment")):
        return "audio_first"
    return "reference_allowed"
