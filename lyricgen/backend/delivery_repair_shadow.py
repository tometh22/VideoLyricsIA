"""Attach bounded Repair Agent proposals to a final transcription result.

This adapter connects the transcription/timing pipeline to Delivery QC without
granting the repair code write access to the job.  It is disabled by default;
``observe`` returns a candidate diff for the editor and never replaces the
machine segments.
"""
from __future__ import annotations

from copy import deepcopy
import os
from typing import Any, Mapping


_ENABLED_MODES = {"observe"}


def _mode() -> str:
    value = os.environ.get("DELIVERY_REPAIR_SHADOW_MODE", "off")
    return str(value or "off").strip().lower()


def _duration(result: Mapping[str, Any]) -> float | None:
    quality = result.get("transcription_quality") or {}
    metrics = quality.get("metrics") or {}
    try:
        value = float(metrics.get("audio_duration_s"))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _reference_lines(value: Any) -> list[str]:
    return [line.strip() for line in str(value or "").splitlines() if line.strip()]


def attach_delivery_repair_shadow(
    result: dict,
    *,
    artist: str = "",
    title: str = "",
    filename: str = "",
    is_live: bool = False,
) -> dict:
    """Return ``result`` plus a review-only repair diff when evidence permits.

    The source result and its segments are never mutated.  An unattested
    catalogue candidate produces an explicit abstention instead of spelling
    suggestions, preventing a wrong-song lookup from becoming an auto-fix.
    """
    if not isinstance(result, dict) or _mode() not in _ENABLED_MODES:
        return result

    output = dict(result)
    from word_line_boundary_diagnostics import analyze_word_line_boundaries
    boundary_diagnostics = analyze_word_line_boundaries(
        deepcopy(result.get("segments") or []),
    )
    from structural_t4_shadow import build_structural_t4_shadow
    structural_t4 = build_structural_t4_shadow(
        deepcopy(result.get("segments") or []),
    )

    def attach(shadow: dict) -> dict:
        shadow = {
            **shadow,
            "t4_word_line_boundaries": deepcopy(boundary_diagnostics),
            "t4_structural_shadow": deepcopy(structural_t4),
        }
        # `transcription_quality` is the persisted editor-facing envelope in
        # the async worker. Keep the top-level copy for direct/legacy callers
        # and the nested copy so polling clients can actually render the diff.
        output["delivery_repair_shadow"] = shadow
        quality = dict(output.get("transcription_quality") or {})
        quality["delivery_repair_shadow"] = deepcopy(shadow)
        output["transcription_quality"] = quality
        return output

    attestation = result.get("reference_attestation")
    reference_lines = _reference_lines(result.get("reference_lyrics"))
    reference_supported = bool(
        isinstance(attestation, Mapping)
        and attestation.get("allow_vocabulary_reconciliation")
    )
    if not reference_lines or not reference_supported:
        return attach({
            "schema_version": "genly-delivery-repair-shadow-v1",
            "mode": "observe",
            "status": "ABSTAINED",
            "reason": (
                "reference_unattested" if reference_lines
                else "reference_unavailable"
            ),
            "mutated_output": False,
        })

    from delivery_repair_agent import repair_delivery_manifest

    manifest = {
        "metadata": {"artist": artist, "title": title},
        "asset": {"filename": filename, "duration": _duration(result)},
        "segments": deepcopy(result.get("segments") or []),
        "approved_lyrics": reference_lines,
        "reference_trusted": True,
        "reference_health": dict(attestation),
        "quality": deepcopy(result.get("transcription_quality") or {}),
        "is_live": bool(is_live),
    }
    repair = repair_delivery_manifest(manifest)
    from transcription_quality import segments_hash
    candidate_segments = deepcopy((repair.get("manifest") or {}).get("segments") or [])
    return attach({
        "schema_version": "genly-delivery-repair-shadow-v1",
        "mode": "observe",
        "status": repair.get("status"),
        "summary": deepcopy(repair.get("summary") or {}),
        "actions": deepcopy(repair.get("actions") or []),
        "before_preflight": deepcopy(repair.get("before_preflight") or {}),
        "reference_attestation": deepcopy(dict(attestation)),
        "segments_hash": segments_hash(result.get("segments") or []),
        "editor_review": deepcopy(repair.get("editor_review") or {}),
        "candidate_segments": candidate_segments,
        "requirements_before_delivery": deepcopy(
            repair.get("requirements_before_delivery") or []
        ),
        "mutated_output": False,
    })
