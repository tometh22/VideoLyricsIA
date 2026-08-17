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

from evidence_attestation import verify_artifact


POLICY_VERSION = "lyrics-quality-v5"
_TRUE = {"1", "true", "yes", "on"}
_PIPELINE_CONFIG_KEYS = (
    "TRANSCRIBE_VAD_FIRST", "VAD_CHUNK_ENABLED", "CTC_ALIGN_ENABLED",
    "FORCED_ALIGNER_ENABLED", "ANCHOR_LYRICS_ENABLED",
    "WHISPER_REFERENCE_PROMPT_MODE", "LRCLIB_PLAIN_ALIGNER_ENABLED",
    "TARGETED_CONSENSUS_ENABLED",
    "LIVE_LEXICAL_CONSENSUS_ENABLED", "LIVE_INDEPENDENT_VERIFY_ENABLED",
    "TARGETED_SLOW_STEM_ENABLED", "TARGETED_GEMINI_VERIFY_ENABLED",
    "TARGETED_ACOUSTIC_STRUCTURE_ENABLED", "TARGETED_ACOUSTIC_CTC_ENABLED",
    "TARGETED_STRUCTURAL_AUTOREPAIR_ENABLED",
    "TARGETED_SLOW_STEM_SPEED", "TARGETED_CONSENSUS_MAX_WINDOWS",
    "TARGETED_CONSENSUS_MAX_BILLED_SECONDS",
    "TARGETED_CONSENSUS_MAX_CLIP_SECONDS",
    "TARGETED_CONSENSUS_DEADLINE_SECONDS",
    "LIVE_INDEPENDENT_VERIFY_MAX_SECONDS", "LIVE_ASR_MAX_BILLED_SECONDS",
    "LIVE_INDEPENDENT_MIX_FALLBACK_ENABLED",
    "TARGETED_CTC_PHASE_MAX", "TARGETED_CTC_PHASE_MEDIAN_MAX",
    "TARGETED_CTC_ANCHOR_MAX", "TARGETED_CTC_ANCHORED_STEM_MIN",
    "TARGETED_CTC_ANCHORED_MIX_MIN", "TARGETED_CTC_PHASE_MARGIN_MIN",
    "CTC_ALIGN_MODEL", "CTC_ALIGN_MODEL_REVISION", "CTC_ALIGN_STAR_DELTA",
    "CTC_ALIGN_MAX_AUDIO_S",
    "CTC_ALIGN_WORD_SEP", "CTC_ALIGN_SKIP_ARCS", "CTC_ALIGN_SKIP_LAMBDA",
    "CTC_ALIGN_MAX_SKIP_FRAC", "CTC_ALIGN_MIN_MED_SCORE",
    "CTC_ALIGN_MIX_ACCEPT", "CTC_ALIGN_MIX_ACCEPT_KNOWN",
    "CTC_ALIGN_MIX_RECOVER", "CTC_ALIGN_EDGE_SNAP",
    "CTC_ALIGN_EDGE_SNAP_SCORE", "QUALITY_CTC_CALIBRATION_SHA256",
    "TARGETED_CTC_STEM_SCORE_MIN", "TARGETED_CTC_MIX_SCORE_MIN",
    "TARGETED_ACOUSTIC_STEM_DTW_MAX", "TARGETED_ACOUSTIC_MIX_DTW_MAX",
    "TARGETED_ACOUSTIC_STEM_BOUNDARY_MIN",
    "TARGETED_ACOUSTIC_MIX_BOUNDARY_MIN", "TARGETED_ACOUSTIC_PERIOD_CV_MAX",
    "DEMUCS_MODEL", "DEMUCS_MODEL_VERSION", "DEMUCS_MODEL_CHECKSUM",
    "DEMUCS_VARIANT", "REPLICATE_DEMUCS_MODEL", "VOCAL_SEP_ENABLED",
    "QUALITY_ASR_USD_PER_MINUTE",
    "QUALITY_OPENAI_ASR_USD_PER_MINUTE",
    "QUALITY_GEMINI_AUDIO_USD_PER_MINUTE",
    "TARGETED_ACOUSTIC_VOICE_CHAIN_GAP_MAX",
    "TARGETED_STRUCTURAL_AUTOREPAIR_MODE",
    "TRANSCRIPTION_QUALITY_CALIBRATED",
    "TRANSCRIPTION_QUALITY_INLINE_RETRY",
)

RELEASE_REPORT_REQUIRED_CHECKS = frozenset({
    "corpus_50", "split_30_20", "cohorts_20_20_10",
    "repetition_adlib_coverage", "crowd_chorus_coverage",
    "pericos_six_events", "event_count_f1_global", "event_count_f1_live",
    "event_count_f1_holdout", "vocalization_recall_global",
    "vocalization_recall_live", "vocalization_recall_holdout",
    "timing_onset_p90", "timing_end_p90", "timing_holdout_p90",
    "wer_non_regression", "wer_holdout_non_regression",
    "candidate_runtime_config_bound", "shadow_ledger_attested",
    "shadow_counts_consistent", "shadow_bound_to_candidate",
    "automatic_precision", "zero_catastrophic_approvals",
    "automatic_coverage", "shadow_volume_and_duration",
    "operator_full_coverage", "operator_p50", "operator_p90",
    "cost_full_coverage", "cost_ci95_below_baseline",
})


def policy_mode() -> str:
    value = os.environ.get("TRANSCRIPTION_QUALITY_MODE", "observe").strip().lower()
    return value if value in {"observe", "enforce"} else "observe"


def effective_policy_mode(*, job_id: str = "", tenant_id: str = "") -> str:
    """Resolve enforce per stable pilot/percentage cohort, never globally."""
    if policy_mode() != "enforce":
        return "observe"
    # Pure unit callers without a production identity retain explicit enforce;
    # every DB-backed render path passes both identifiers below.
    if not job_id and not tenant_id:
        return "enforce"
    pilots = {
        value.strip() for value in os.environ.get(
            "TRANSCRIPTION_QUALITY_ENFORCE_PILOT_TENANTS", "",
        ).split(",") if value.strip()
    }
    if tenant_id and str(tenant_id) in pilots:
        return "enforce"
    try:
        percentage = float(os.environ.get(
            "TRANSCRIPTION_QUALITY_ENFORCE_PERCENT", "0",
        ))
    except (TypeError, ValueError):
        percentage = 0.0
    percentage = max(0.0, min(100.0, percentage))
    bucket = int(hashlib.sha256(str(job_id).encode("utf-8")).hexdigest()[:8], 16)
    return "enforce" if bucket % 10_000 < round(percentage * 100) else "observe"


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


def calibration_identity() -> dict:
    """Require a hash-pinned benchmark report that actually says GO."""
    runtime = runtime_identity()
    calibration_id = os.environ.get(
        "TRANSCRIPTION_QUALITY_CALIBRATION_ID", ""
    ).strip()[:128]
    policy = os.environ.get(
        "TRANSCRIPTION_QUALITY_CALIBRATION_POLICY", ""
    ).strip()
    fingerprint = os.environ.get(
        "TRANSCRIPTION_QUALITY_CALIBRATION_CONFIG_FINGERPRINT", ""
    ).strip()
    enabled = (
        os.environ.get("TRANSCRIPTION_QUALITY_CALIBRATED", "0")
        .strip().lower() in _TRUE
    )
    report_path = os.environ.get(
        "TRANSCRIPTION_QUALITY_RELEASE_REPORT_PATH", "",
    ).strip()
    report_sha = os.environ.get(
        "TRANSCRIPTION_QUALITY_RELEASE_REPORT_SHA256", "",
    ).strip().lower()
    manifest_path = os.environ.get(
        "TRANSCRIPTION_QUALITY_BENCHMARK_MANIFEST_PATH", "",
    ).strip()
    report_valid = False
    report_manifest_sha = None
    if report_path and len(report_sha) == 64:
        try:
            with open(report_path, "rb") as handle:
                raw = handle.read()
            report = json.loads(raw.decode("utf-8"))
            candidate = (report.get("systems") or {}).get("candidate") or {}
            report_manifest_sha = report.get("manifest_sha256")
            checks = (report.get("release_gate") or {}).get("checks") or {}
            attested, _attestation_reason = verify_artifact(
                report, "BENCHMARK_RELEASE_PUBLIC_KEYS",
            )
            with open(manifest_path, "rb") as manifest_handle:
                manifest_raw = manifest_handle.read()
            report_valid = bool(
                hashlib.sha256(raw).hexdigest() == report_sha
                and attested
                and (report.get("release_gate") or {}).get("decision") == "GO"
                and RELEASE_REPORT_REQUIRED_CHECKS.issubset(checks)
                and all(checks.get(name) is True for name in RELEASE_REPORT_REQUIRED_CHECKS)
                and candidate.get("release") == runtime["pipeline_release"]
                and candidate.get("pipeline_config_fingerprint") == fingerprint
                and isinstance(report_manifest_sha, str)
                and len(report_manifest_sha) == 64
                and hashlib.sha256(manifest_raw).hexdigest() == report_manifest_sha
            )
        except (OSError, ValueError, TypeError):
            report_valid = False
    calibrated = bool(
        enabled and calibration_id and policy == POLICY_VERSION
        and fingerprint == runtime["pipeline_config_fingerprint"]
        and report_valid
    )
    return {
        "calibrated": calibrated,
        "calibration_id": calibration_id or None,
        "policy_version": policy or None,
        "config_fingerprint": fingerprint or None,
        "release_report_sha256": report_sha or None,
        "manifest_sha256": report_manifest_sha,
        "method": "benchmark_calibrated_v1" if calibrated
        else "deterministic_guardrails_v1",
    }


def segments_hash(segments: Iterable[dict]) -> str:
    """Hash only render-relevant lyric content, not ephemeral editor fields."""
    canonical = [
        {
            "start": round(_f(segment.get("start")), 6),
            "end": round(_f(segment.get("end")), 6),
            "text": str(segment.get("text") or ""),
            "scale": segment.get("scale"),
            "pos": segment.get("pos"),
            "rot": segment.get("rot"),
            "words": [
                {
                    "start": round(_f(word.get("start")), 6),
                    "end": round(_f(word.get("end")), 6),
                    "word": str(word.get("word") or word.get("text") or ""),
                }
                for word in (segment.get("words") or [])
                if isinstance(word, dict)
            ],
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
        if start < 0 or end <= start + 1e-6:
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
    for window in merged:
        window["id"] = unsafe_window_id(window)
    return merged


def unsafe_window_id(window: dict) -> str:
    canonical = {
        "start": round(_f(window.get("start")), 3),
        "end": round(_f(window.get("end")), 3),
        "reasons": sorted(str(item) for item in (window.get("reasons") or [])),
        "segment_indices": sorted(int(item) for item in (
            window.get("segment_indices") or []
        )),
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return "qw_" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def confirmed_all_windows(quality: dict, confirmed_ids: Iterable[str]) -> bool:
    expected = {
        str(window.get("id") or unsafe_window_id(window))
        for window in (quality.get("unsafe_windows") or [])
        if isinstance(window, dict)
    }
    confirmed = {str(value) for value in (confirmed_ids or []) if value}
    return expected == confirmed


def supersede_pending_analysis(
    quality: dict | None, *, revision: int, segments: list[dict] | None = None,
) -> dict | None:
    """Invalidate every quality artifact when a human changes the snapshot.

    Even a terminal verdict is stale after a text/timing edit.  Preserve only
    immutable runtime identity and the *bounds* that still need re-analysis;
    acoustic evidence, scores, acknowledgements and fingerprints must never
    cross a segment hash boundary.
    """
    if not isinstance(quality, dict):
        return quality
    carried = []
    for window in quality.get("unsafe_windows") or []:
        if not isinstance(window, dict):
            continue
        start, end = _f(window.get("start")), _f(window.get("end"))
        if end > start:
            carried.append({
                "start": start, "end": end,
                "reasons": [
                    *(window.get("reasons") or [window.get("reason") or "unsafe"]),
                    "superseded_quality_window",
                ],
                "segment_indices": window.get("segment_indices") or [],
            })
    if segments is not None:
        carried.extend(build_unsafe_windows(segments, []))
    windows = _merge_windows(carried, pad_s=0.0)
    identity_keys = {
        "schema_version", "version", "quality_version", "policy_version",
        "pipeline_release", "pipeline_config_fingerprint", "timing_source",
        "audio_sha256", "mode",
    }
    updated = {key: quality[key] for key in identity_keys if key in quality}
    updated.update({
        "decision": "review_required", "render_blocked": True,
        "analysis_pending": False,
        "analysis_status": "superseded_by_edit",
        "analysis_superseded_revision": int(revision),
        "evaluated_revision": int(revision),
        "segments_hash": segments_hash(segments or []),
        "unsafe_windows": windows,
        "reasons": [{
            "code": "quality_analysis_superseded_by_edit",
            "severity": "critical", "value": int(revision),
        }],
    })
    return updated


_NON_OVERRIDABLE_REASONS = {
    "empty_transcription", "empty_lyric_lines", "timeline_inversion",
    "invalid_timing_range", "duplicate_line_starts",
    "quality_evidence_unavailable", "quality_retry_failed",
    "quality_analysis_superseded_by_edit", "quality_analysis_enqueue_failed",
}


def manual_override_allowed(quality: dict) -> bool:
    if quality.get("decision") == "retry_failed":
        return False
    codes = {
        str(reason.get("code")) for reason in (quality.get("reasons") or [])
        if isinstance(reason, dict)
    }
    return not bool(codes & _NON_OVERRIDABLE_REASONS)


def quality_fingerprint(quality: dict, *, revision: int,
                        content_hash: str) -> str:
    """Bind an acknowledgement to the exact evidence it reviewed."""
    windows = sorted(
        str(window.get("id") or unsafe_window_id(window))
        for window in (quality.get("unsafe_windows") or [])
        if isinstance(window, dict)
    )
    reasons = sorted(
        (
            str(reason.get("code")),
            str(reason.get("severity")),
            json.dumps(
                reason.get("value"), ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), default=str,
            ),
        )
        for reason in (quality.get("reasons") or [])
        if isinstance(reason, dict)
    )
    payload = {
        "revision": int(revision), "segments_hash": content_hash,
        "policy_version": quality.get("policy_version"),
        "pipeline_release": quality.get("pipeline_release"),
        "pipeline_config_fingerprint": quality.get("pipeline_config_fingerprint"),
        "decision": quality.get("decision"), "reasons": reasons,
        "window_ids": windows, "calibration": quality.get("risk_calibration"),
        "timing_source": quality.get("timing_source"),
        "evidence": {
            "metrics": quality.get("metrics"),
            "retry": quality.get("retry"),
            "acoustic_evidence": quality.get("acoustic_evidence"),
            "analysis_windows": quality.get("analysis_windows"),
        },
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def build_unsafe_windows(segments: list[dict], words: list[dict], *,
                         voiced_gaps: list[dict] | None = None,
                         independent_words: list[dict] | None = None,
                         lexical_unverified: list[dict] | None = None,
                         structural_disagreements: list[dict] | None = None,
                         evidence_view_disagreements: list[dict] | None = None) -> list[dict]:
    """Locate bounded areas worth a second, independent ASR pass."""
    from audio_coverage import text_mismatches, uncovered_spans

    windows: list[dict] = []
    from line_evidence import evidence_issues
    for issue in evidence_issues(segments):
        windows.append({
            **issue,
            "start": min(
                _f(issue.get("start")), _f(issue.get("source_start"), _f(issue.get("start"))),
            ),
            "end": max(
                _f(issue.get("end")), _f(issue.get("source_end"), _f(issue.get("end"))),
            ),
        })
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
    for item in evidence_view_disagreements or []:
        windows.append({
            "start": item.get("start"), "end": item.get("end"),
            "reason": "stem_mix_evidence_disagreement",
        })
    return _merge_windows(windows)


def evaluate(segments: list[dict], coverage: dict | None, *,
             unsafe_windows: list[dict] | None = None,
             retry_stats: dict | None = None,
             require_independent: bool = False,
             acoustic_evidence: dict | None = None,
             resolved_reason_counts: dict[str, int] | None = None) -> dict:
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
    resolved_reason_counts = resolved_reason_counts or {}
    timeline = timeline_issues(segments)
    reasons: list[dict] = []

    def add(code: str, severity: str, value, deduction: int) -> None:
        reasons.append({"code": code, "severity": severity, "value": value})

    if timeline["start_inversions"]:
        add("timeline_inversion", "critical", timeline["start_inversions"], 50)
    if timeline["invalid_ranges"]:
        add("invalid_timing_range", "critical", timeline["invalid_ranges"], 50)
    if timeline["empty_text"]:
        add("empty_lyric_lines", "critical", timeline["empty_text"], 35)
    if timeline["duplicate_starts"]:
        add("duplicate_line_starts", "critical", timeline["duplicate_starts"], 30)
    if timeline["severe_overlaps"]:
        # The current editor/render model is one linear lyric lane and has no
        # overlap_group representation. A >500ms overlap is destructive here.
        add("severe_line_overlaps", "critical", timeline["severe_overlaps"], 40)
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
    lexical_unverified = max(
        0, int(coverage.get("live_lexical_unverified") or 0)
        - int(resolved_reason_counts.get("live_lexical_unverified") or 0),
    )
    if lexical_unverified:
        add(
            "live_lexical_unverified", "critical",
            lexical_unverified, min(45, 25 * lexical_unverified),
        )
    structural_disagreements = max(
        0, int(coverage.get("live_structural_disagreements") or 0)
        - int(resolved_reason_counts.get("live_structural_disagreement") or 0),
    )
    if structural_disagreements:
        # A repeated refrain whose acoustically observed structure disagrees
        # with the delivered rows is not render-safe.  Coverage can still be
        # 100% because it measures whether words exist near a row, not whether
        # the refrain has the correct cardinality or complete phrase.  Marking
        # this critical both activates the bounded consensus retry and keeps a
        # still-unresolved result out of rendering.
        add(
            "live_structural_disagreement", "critical",
            structural_disagreements,
            min(45, 15 + 5 * structural_disagreements),
        )
    evidence_view_disagreements = int(
        coverage.get("stem_mix_evidence_disagreements") or 0
    )
    if evidence_view_disagreements:
        add(
            "stem_mix_evidence_disagreement", "critical",
            evidence_view_disagreements, 35,
        )
    from line_evidence import evidence_issues
    line_issue_counts: dict[str, int] = {}
    for issue in evidence_issues(segments):
        for code in issue.get("reasons") or []:
            line_issue_counts[str(code)] = line_issue_counts.get(str(code), 0) + 1
    line_issue_severity = {
        "provider_timing_collapsed": "critical",
        "low_ctc_timing_confidence": "critical",
        "low_asr_content_confidence": "warning",
        "text_word_cardinality_mismatch": "critical",
        "isolated_tail_low_support": "critical",
    }
    for code, count in sorted(line_issue_counts.items()):
        add(code, line_issue_severity.get(code, "warning"), count, 30)
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

    # A v4 structural row may carry review=False because the old gate treated
    # lexical-anchor support as proof of the whole vocalization.  v5 never
    # inherits that approval: until benchmark calibration, every such rewrite
    # is explicitly review-scoped.
    structural_rows = sum(
        1 for segment in segments
        if isinstance(segment, dict)
        and (segment.get("structural_hybrid") or segment.get("structural_repair"))
    )
    if structural_rows:
        add(
            "structural_autorepair_uncalibrated", "critical",
            structural_rows, 45,
        )

    audio_cov = _f(coverage.get("audio_coverage"), 1.0)
    if audio_cov < 0.80:
        add("low_audio_coverage", "critical", round(audio_cov, 4), 35)
    elif audio_cov < 0.90:
        add("soft_audio_coverage", "warning", round(audio_cov, 4), 15)

    mismatches = max(
        0, int(coverage.get("text_mismatches") or 0)
        - int(resolved_reason_counts.get("text_mismatch") or 0),
    )
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

    acoustic_evidence = acoustic_evidence or {}
    evidence_windows = acoustic_evidence.get("windows") or [acoustic_evidence]
    for evidence_window in evidence_windows:
        mapping = evidence_window.get("content_mapping") or {}
        structure = evidence_window.get("acoustic_structure") or {}
        if structure and not structure.get("accepted"):
            add(
                "acoustic_structure_unavailable", "critical",
                structure.get("reason") or "declined", 40,
            )
        if mapping:
            if int(mapping.get("strong_unassigned_events") or 0):
                add(
                    "strong_unassigned_vocal_events", "critical",
                    int(mapping.get("strong_unassigned_events") or 0), 45,
                )
            if not mapping.get("accepted"):
                add(
                    "acoustic_mapping_ambiguous", "critical",
                    mapping.get("reason") or mapping.get("margin"), 35,
                )

    retry_stats = retry_stats or {"attempted": False}
    if retry_stats.get("failed"):
        add(
            "quality_retry_failed", "critical",
            retry_stats.get("failure_reason") or "unknown", 40,
        )
    if int(retry_stats.get("windows_skipped") or 0) > 0:
        add(
            "quality_windows_unprocessed", "critical",
            int(retry_stats.get("windows_skipped") or 0), 35,
        )
    if int(retry_stats.get("windows_truncated") or 0) > 0:
        add(
            "quality_windows_truncated", "critical",
            int(retry_stats.get("windows_truncated") or 0), 35,
        )

    # Windows are actionable product state, not merely diagnostics.  A clean
    # retry may remove them; any window still present must be confirmed by the
    # operator for this exact revision before render.
    unsafe_windows = list(unsafe_windows or [])
    if unsafe_windows and not any(
        reason["code"] in {
            "timeline_inversion", "invalid_timing_range", "empty_transcription",
            "quality_evidence_unavailable", "quality_retry_failed",
        }
        for reason in reasons
    ):
        add("unsafe_windows_pending_review", "critical", len(unsafe_windows), 30)

    # Capture the counterfactual shadow decision *before* the rollout
    # calibration blocker is added. This lets us measure what the candidate
    # would have approved without enabling it, while incomplete/failed
    # analyses remain ineligible rather than becoming convenient negatives.
    ineligible_codes = {
        "quality_evidence_unavailable", "quality_retry_failed",
        "quality_windows_unprocessed", "quality_windows_truncated",
        "acoustic_structure_unavailable",
    }
    shadow_eligible = not any(
        reason["code"] in ineligible_codes for reason in reasons
    )
    shadow_would_approve = bool(
        shadow_eligible
        and not any(reason["severity"] == "critical" for reason in reasons)
    )
    shadow_decision = {
        "eligible": shadow_eligible,
        "would_approve": shadow_would_approve,
        "reason_codes": [reason["code"] for reason in reasons],
    }

    calibration = calibration_identity()
    if not calibration["calibrated"]:
        add(
            "quality_calibration_unavailable", "critical",
            calibration.get("calibration_id") or "missing", 0,
        )

    blocking = any(reason["severity"] == "critical" for reason in reasons)
    decision = (
        "retry_failed" if retry_stats.get("failed")
        else "review_required" if blocking else "pass"
    )

    dimension_codes = {
        "text": {
            "quality_evidence_unavailable", "text_audio_mismatch",
            "independent_text_audio_mismatch", "live_lexical_unverified",
            "empty_lyric_lines", "empty_transcription",
            "low_asr_content_confidence", "isolated_tail_low_support",
            "text_word_cardinality_mismatch",
        },
        "event_count": {
            "live_structural_disagreement", "acoustic_mapping_ambiguous",
            "strong_unassigned_vocal_events", "structural_autorepair_uncalibrated",
            "quality_windows_unprocessed", "quality_windows_truncated",
            "provider_timing_collapsed", "text_word_cardinality_mismatch",
            "stem_mix_evidence_disagreement",
        },
        "timing": {
            "severe_line_overlaps", "duplicate_line_starts",
            "invalid_timing_range", "timeline_inversion",
            "low_ctc_timing_confidence", "provider_timing_collapsed",
        },
        "vocal_coverage": {
            "low_audio_coverage", "soft_audio_coverage", "voiced_gap",
            "short_voiced_gap", "uncovered_asr_audio", "short_uncovered_audio",
            "low_independent_coverage", "soft_independent_coverage",
            "independent_uncovered_audio", "short_independent_uncovered_audio",
            "stem_mix_evidence_disagreement",
        },
        "timeline_integrity": {
            "timeline_inversion", "invalid_timing_range", "duplicate_line_starts",
            "severe_line_overlaps",
        },
    }
    severity_risk = {"critical": .92, "warning": .35}
    risks = {}
    for dimension, codes in dimension_codes.items():
        values = [severity_risk.get(reason["severity"], 0.0)
                  for reason in reasons if reason["code"] in codes]
        risks[dimension] = round(max(values, default=0.0), 4)
    # Hard timeline defects are deterministic and therefore probability one.
    if timeline["start_inversions"] or timeline["invalid_ranges"]:
        risks["timing"] = 1.0
        risks["timeline_integrity"] = 1.0
    overall_risk = max([*risks.values(), .92 if blocking else 0.0], default=0.0)
    evidence_lineage = sorted({
        str(source)
        for segment in segments if isinstance(segment, dict)
        for source in (
            list(segment.get("consensus_sources") or [])
            + [segment.get("content_source")]
            + [((segment.get("provider_evidence") or {}).get("source"))]
        )
        if source
    })
    return {
        "policy_version": POLICY_VERSION,
        "mode": policy_mode(),
        "decision": decision,
        "score": (
            round(100.0 * (1.0 - overall_risk), 1)
            if calibration["calibrated"] else None
        ),
        "risk": round(overall_risk, 4),
        "risk_dimensions": risks,
        "risk_calibration": calibration,
        "render_blocked": blocking,
        "reasons": reasons,
        "metrics": {**coverage, **timeline},
        "unsafe_windows": unsafe_windows,
        "shadow_decision": shadow_decision,
        "retry": retry_stats,
        "acoustic_evidence": acoustic_evidence,
        "evidence_lineage": evidence_lineage,
        "segments_hash": segments_hash(segments),
        **runtime_identity(),
    }


def can_render(quality: dict | None, *, revision: int,
               segments: list[dict] | None = None, job_id: str = "",
               tenant_id: str = "") -> tuple[bool, str | None]:
    """Render policy. Old jobs and observe mode remain backward compatible."""
    runtime_mode = effective_policy_mode(job_id=job_id, tenant_id=tenant_id)
    if not quality:
        if runtime_mode == "enforce":
            return False, "transcription_quality_unavailable"
        return True, None
    # Runtime observe is the authoritative emergency kill switch. Persisted
    # evidence from an earlier enforce rollout must never keep blocking jobs.
    if runtime_mode != "enforce":
        return True, None
    if quality.get("analysis_pending") or str(
        quality.get("analysis_status") or ""
    ).lower() in {"pending", "superseded_by_edit", "failed", "retry_failed"}:
        return False, "transcription_quality_analysis_incomplete"
    ack = quality.get("acknowledgement") or {}
    current_hash = segments_hash(segments or [])
    quality_is_current = (
        quality.get("policy_version") == POLICY_VERSION
        and
        quality.get("pipeline_release") == runtime_identity()["pipeline_release"]
        and
        quality.get("pipeline_config_fingerprint")
        == runtime_identity()["pipeline_config_fingerprint"]
        and
        quality.get("segments_hash") == current_hash
        and int(quality.get("evaluated_revision", -1)) == int(revision)
    )
    persisted_calibration = quality.get("risk_calibration") or {}
    current_calibration = calibration_identity()
    calibrated = bool(
        persisted_calibration.get("calibrated")
        and current_calibration.get("calibrated")
        and persisted_calibration.get("calibration_id")
        == current_calibration.get("calibration_id")
        and persisted_calibration.get("release_report_sha256")
        == current_calibration.get("release_report_sha256")
        and persisted_calibration.get("manifest_sha256")
        == current_calibration.get("manifest_sha256")
    )
    current_fingerprint = quality_fingerprint(
        quality, revision=revision, content_hash=current_hash,
    )
    fingerprint_is_current = (
        quality.get("quality_fingerprint") == current_fingerprint
    )
    if (quality.get("decision") == "pass" and quality_is_current
            and calibrated and fingerprint_is_current):
        return True, None
    if (quality_is_current
            and manual_override_allowed(quality)
            and int(ack.get("revision", -1)) == int(revision)
            and ack.get("segments_hash") == current_hash
            and quality.get("policy_version") == POLICY_VERSION
            and ack.get("policy_version") == POLICY_VERSION
            and ack.get("quality_fingerprint") == current_fingerprint
            and confirmed_all_windows(
                quality, ack.get("confirmed_window_ids") or [],
            )):
        return True, None
    return False, "transcription_quality_review_required"
