"""Deterministic quality gate for lyric transcription results.

The transcription cascade has several independent quality signals.  This
module turns them into one persisted decision that every consumer can use.
It is deliberately pure: no database, network or model calls live here.
"""
from __future__ import annotations

import os
import math
import hashlib
import json
from typing import Iterable


POLICY_VERSION = "lyrics-quality-v2"
_TRUE = {"1", "true", "yes", "on"}
_PIPELINE_CONFIG_KEYS = (
    "TRANSCRIBE_VAD_FIRST", "VAD_CHUNK_ENABLED", "CTC_ALIGN_ENABLED",
    "FORCED_ALIGNER_ENABLED", "ANCHOR_LYRICS_ENABLED",
    "WHISPER_REFERENCE_PROMPT_MODE", "LRCLIB_PLAIN_ALIGNER_ENABLED",
    "TARGETED_CONSENSUS_ENABLED", "TRANSCRIPTION_QUALITY_MODE",
    "LIVE_LEXICAL_CONSENSUS_ENABLED", "LIVE_INDEPENDENT_VERIFY_ENABLED",
    "TARGETED_SLOW_STEM_ENABLED", "TARGETED_GEMINI_VERIFY_ENABLED",
    "TARGETED_STRUCTURAL_AUTOREPAIR_ENABLED",
    "TARGETED_SLOW_STEM_SPEED", "TARGETED_CONSENSUS_MAX_WINDOWS",
    "TARGETED_CONSENSUS_MAX_BILLED_SECONDS",
    "TARGETED_CONSENSUS_MAX_CLIP_SECONDS",
    "TARGETED_CONSENSUS_DEADLINE_SECONDS",
    "LIVE_INDEPENDENT_VERIFY_MAX_SECONDS", "LIVE_ASR_MAX_BILLED_SECONDS",
    "LIVE_INDEPENDENT_MIX_FALLBACK_ENABLED",
)


def policy_mode() -> str:
    value = os.environ.get("TRANSCRIPTION_QUALITY_MODE", "observe").strip().lower()
    return value if value in {"observe", "enforce"} else "observe"


def runtime_identity() -> dict:
    """Immutable release/config identity for operational KPI cohorts."""
    config = {key: os.environ.get(key, "") for key in _PIPELINE_CONFIG_KEYS}
    encoded = json.dumps(config, sort_keys=True, separators=(",", ":"))
    return {
        "pipeline_release": (
            os.environ.get("RELEASE")
            or os.environ.get("RAILWAY_GIT_COMMIT_SHA")
            or "unknown"
        )[:64],
        "pipeline_config_fingerprint": hashlib.sha256(
            encoded.encode("utf-8")
        ).hexdigest()[:16],
    }


def segments_hash(segments: Iterable[dict]) -> str:
    """Hash only render-relevant lyric content, not ephemeral editor fields."""
    canonical = [
        {
            "start": round(_f(segment.get("start")), 6),
            "end": round(_f(segment.get("end")), 6),
            "text": str(segment.get("text") or ""),
        }
        for segment in (segments or []) if isinstance(segment, dict)
    ]
    payload = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _f(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def timeline_issues(segments: Iterable[dict]) -> dict:
    """Return defects that can make player selection move backwards."""
    inversions = invalid_ranges = empty_text = duplicate_starts = severe_overlaps = 0
    previous_start = -1.0
    previous_end = -1.0
    for segment in segments or []:
        if not isinstance(segment, dict):
            invalid_ranges += 1
            continue
        start, end = _f(segment.get("start")), _f(segment.get("end"))
        if not (math.isfinite(start) and math.isfinite(end)):
            invalid_ranges += 1
            continue
        if start + 1e-6 < previous_start:
            inversions += 1
        if abs(start - previous_start) <= 1e-3:
            duplicate_starts += 1
        if previous_end - start > 0.50:
            severe_overlaps += 1
        if start < 0 or end + 1e-6 < start:
            invalid_ranges += 1
        if not str(segment.get("text") or "").strip():
            empty_text += 1
        previous_start = start
        previous_end = end
    return {
        "start_inversions": inversions,
        "invalid_ranges": invalid_ranges,
        "empty_text": empty_text,
        "duplicate_starts": duplicate_starts,
        "severe_overlaps": severe_overlaps,
    }


def _merge_windows(windows: list[dict], *, pad_s: float = 1.5) -> list[dict]:
    ordered = sorted(
        (dict(w) for w in windows if _f(w.get("end")) > _f(w.get("start"))),
        key=lambda w: _f(w.get("start")),
    )
    merged: list[dict] = []
    for window in ordered:
        start = max(0.0, _f(window.get("start")) - pad_s)
        end = _f(window.get("end")) + pad_s
        reasons = set(window.get("reasons") or [window.get("reason") or "unsafe"])
        indices = set(window.get("segment_indices") or [])
        if window.get("segment_index") is not None:
            indices.add(int(window["segment_index"]))
        if merged and start <= merged[-1]["end"] + 0.5:
            merged[-1]["end"] = round(max(merged[-1]["end"], end), 2)
            merged[-1]["reasons"] = sorted(set(merged[-1]["reasons"]) | reasons)
            merged[-1]["segment_indices"] = sorted(
                set(merged[-1]["segment_indices"]) | indices
            )
        else:
            merged.append({
                "start": round(start, 2), "end": round(end, 2),
                "reasons": sorted(reasons), "segment_indices": sorted(indices),
            })
    return merged


def build_unsafe_windows(segments: list[dict], words: list[dict], *,
                         voiced_gaps: list[dict] | None = None,
                         independent_words: list[dict] | None = None,
                         lexical_unverified: list[dict] | None = None,
                         structural_disagreements: list[dict] | None = None) -> list[dict]:
    """Locate bounded areas worth a second, independent ASR pass."""
    from audio_coverage import text_mismatches, uncovered_spans

    windows: list[dict] = []
    for item in text_mismatches(segments, words):
        windows.append({
            "start": item["start"], "end": item["end"],
            "reason": "text_mismatch", "segment_index": item["index"],
        })
    if independent_words:
        for item in text_mismatches(segments, independent_words):
            windows.append({
                "start": item["start"], "end": item["end"],
                "reason": "independent_text_mismatch",
                "segment_index": item["index"],
            })
        for start, end, _count in uncovered_spans(segments, independent_words):
            windows.append({
                "start": start, "end": end,
                "reason": "independent_uncovered_asr",
            })
    for item in lexical_unverified or []:
        windows.append({
            "start": item.get("start"), "end": item.get("end"),
            "reason": "live_lexical_unverified",
            "segment_index": item.get("index"),
        })
    for item in structural_disagreements or []:
        # Structural disagreements need enough context to hear a repeated
        # refrain as a unit. The catalogue is only a trigger; replacement
        # still requires independent acoustic agreement downstream.
        windows.append({
            "start": max(0.0, _f(item.get("start")) - 5.0),
            "end": _f(item.get("end")) + 15.0,
            "reason": "live_structural_disagreement",
            "segment_index": item.get("index"),
        })
    for start, end, _count in uncovered_spans(segments, words):
        windows.append({"start": start, "end": end, "reason": "uncovered_asr"})
    for gap in voiced_gaps or []:
        windows.append({
            "start": gap.get("start"), "end": gap.get("end"),
            "reason": "voiced_gap",
        })
    return _merge_windows(windows)


def evaluate(segments: list[dict], coverage: dict | None, *,
             unsafe_windows: list[dict] | None = None,
             retry_stats: dict | None = None,
             require_independent: bool = False) -> dict:
    """Evaluate output and return a serializable, explainable verdict."""
    required_evidence = {
        "audio_coverage", "uncovered_seconds", "text_mismatches",
        "voiced_gap_s",
    }
    evidence_available = (
        isinstance(coverage, dict)
        and required_evidence.issubset(coverage)
        and all(math.isfinite(_f(coverage.get(key), float("nan")))
                for key in required_evidence)
    )
    coverage = coverage or {}
    timeline = timeline_issues(segments)
    reasons: list[dict] = []
    score = 100

    def add(code: str, severity: str, value, deduction: int) -> None:
        nonlocal score
        reasons.append({"code": code, "severity": severity, "value": value})
        score -= deduction

    if timeline["start_inversions"]:
        add("timeline_inversion", "critical", timeline["start_inversions"], 50)
    if timeline["invalid_ranges"]:
        add("invalid_timing_range", "critical", timeline["invalid_ranges"], 50)
    if timeline["empty_text"]:
        add("empty_lyric_lines", "critical", timeline["empty_text"], 35)
    if timeline["duplicate_starts"]:
        add("duplicate_line_starts", "critical", timeline["duplicate_starts"], 30)
    if timeline["severe_overlaps"]:
        # Harmonies and call-and-response can overlap legitimately. Surface it
        # for review/benchmarking but only inversions/duplicate starts are
        # selector-breaking by themselves.
        add("severe_line_overlaps", "warning", timeline["severe_overlaps"], 10)
    if not segments:
        add("empty_transcription", "critical", 0, 50)
    if not evidence_available:
        add("quality_evidence_unavailable", "critical", True, 40)
    independent_words = int(coverage.get("independent_witness_words") or 0)
    independent_required_fields = {
        "independent_audio_coverage", "independent_text_mismatches",
        "independent_uncovered_seconds", "audio_duration_s",
    }
    independent_fields_available = (
        independent_required_fields.issubset(coverage)
        and all(math.isfinite(_f(coverage.get(key), float("nan")))
                for key in independent_required_fields)
    )
    if require_independent and (
        independent_words < 8 or not independent_fields_available
    ):
        add("independent_witness_unavailable", "critical", independent_words, 40)
    audio_duration = _f(coverage.get("audio_duration_s"))
    witness_density = (
        independent_words * 60.0 / audio_duration
        if audio_duration > 0 else 0.0
    )
    if require_independent and witness_density < 4.0:
        add(
            "independent_witness_too_sparse", "critical",
            round(witness_density, 3), 35,
        )
    independent_mismatches = int(
        coverage.get("independent_text_mismatches") or 0
    )
    if independent_mismatches:
        add(
            "independent_text_audio_mismatch", "critical",
            independent_mismatches, min(45, 20 * independent_mismatches),
        )
    independent_cov = coverage.get("independent_audio_coverage")
    if require_independent and independent_cov is not None:
        independent_cov = _f(independent_cov)
        if independent_cov < 0.70:
            add("low_independent_coverage", "critical", round(independent_cov, 4), 30)
        elif independent_cov < 0.80:
            add("soft_independent_coverage", "warning", round(independent_cov, 4), 10)
    independent_uncovered = _f(
        coverage.get("independent_uncovered_seconds")
    )
    if independent_uncovered >= 8.0:
        add(
            "independent_uncovered_audio", "critical",
            round(independent_uncovered, 2), 30,
        )
    elif independent_uncovered >= 3.0:
        add(
            "short_independent_uncovered_audio", "warning",
            round(independent_uncovered, 2), 10,
        )
    lexical_unverified = int(coverage.get("live_lexical_unverified") or 0)
    if lexical_unverified:
        add(
            "live_lexical_unverified", "critical",
            lexical_unverified, min(45, 25 * lexical_unverified),
        )
    pending_insertions = sum(
        1 for segment in segments
        if isinstance(segment, dict)
        and segment.get("consensus_reprocessed")
        and segment.get("review")
    )
    if pending_insertions:
        add(
            "consensus_insertions_pending_review", "critical",
            pending_insertions, 35,
        )

    audio_cov = _f(coverage.get("audio_coverage"), 1.0)
    if audio_cov < 0.80:
        add("low_audio_coverage", "critical", round(audio_cov, 4), 35)
    elif audio_cov < 0.90:
        add("soft_audio_coverage", "warning", round(audio_cov, 4), 15)

    mismatches = int(coverage.get("text_mismatches") or 0)
    if mismatches:
        add("text_audio_mismatch", "critical", mismatches, min(45, 25 * mismatches))
    voiced_s = _f(coverage.get("voiced_gap_s"))
    if voiced_s >= 10.0:
        add("voiced_gap", "critical", round(voiced_s, 2), 35)
    elif voiced_s >= 3.0:
        add("short_voiced_gap", "warning", round(voiced_s, 2), 15)
    uncovered_s = _f(coverage.get("uncovered_seconds"))
    if uncovered_s >= 8.0:
        add("uncovered_asr_audio", "critical", round(uncovered_s, 2), 30)
    elif uncovered_s >= 3.0:
        add("short_uncovered_audio", "warning", round(uncovered_s, 2), 10)

    blocking = any(reason["severity"] == "critical" for reason in reasons)
    decision = "review_required" if blocking else "pass"
    return {
        "policy_version": POLICY_VERSION,
        "mode": policy_mode(),
        "decision": decision,
        "score": max(0, score),
        "render_blocked": blocking,
        "reasons": reasons,
        "metrics": {**coverage, **timeline},
        "unsafe_windows": list(unsafe_windows or []),
        "retry": retry_stats or {"attempted": False},
        "segments_hash": segments_hash(segments),
        **runtime_identity(),
    }


def can_render(quality: dict | None, *, revision: int,
               segments: list[dict] | None = None) -> tuple[bool, str | None]:
    """Render policy. Old jobs and observe mode remain backward compatible."""
    if not quality:
        if policy_mode() == "enforce":
            return False, "transcription_quality_unavailable"
        return True, None
    if quality.get("mode") != "enforce" and policy_mode() != "enforce":
        return True, None
    ack = quality.get("acknowledgement") or {}
    current_hash = segments_hash(segments or [])
    quality_is_current = (
        quality.get("policy_version") == POLICY_VERSION
        and
        quality.get("pipeline_config_fingerprint")
        == runtime_identity()["pipeline_config_fingerprint"]
        and
        quality.get("segments_hash") == current_hash
        and int(quality.get("evaluated_revision", -1)) == int(revision)
    )
    if quality.get("decision") == "pass" and quality_is_current:
        return True, None
    if (int(ack.get("revision", -1)) == int(revision)
            and ack.get("segments_hash") == current_hash
            and quality.get("policy_version") == POLICY_VERSION
            and ack.get("policy_version") == POLICY_VERSION):
        return True, None
    return False, "transcription_quality_review_required"
