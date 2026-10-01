"""Transactional repair agent for delivery preflight findings.

The agent edits structured delivery data, never an already encoded video.  Each
candidate repair is applied to a copy and accepted only when a fresh preflight
removes risk without introducing a new issue.  Ambiguous repairs remain explicit
proposals for an acoustic verifier or operator.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Callable, Mapping, MutableMapping, Union

from delivery_preflight import build_delivery_preflight


SCHEMA_VERSION = "genly-delivery-repair-v1"
LexicalVerifier = Callable[[Mapping[str, Any]], Union[Mapping[str, Any], bool, float]]
TimingVerifier = Callable[[Mapping[str, Any]], Union[Mapping[str, Any], bool, float]]


@dataclass(frozen=True)
class RepairPolicy:
    trusted_typo_min_match: float = 0.90
    verifier_min_confidence: float = 0.85
    max_safe_overlap_s: float = 0.30
    min_segment_duration_s: float = 0.12
    auto_small_overlap: bool = False
    auto_outside_asset: bool = True
    auto_verified_endpoints: bool = False
    verified_endpoint_min_score: float = 0.90


def _stable_id(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()[:16]


def _risk_vector(report: Mapping[str, Any]) -> tuple[int, int, int]:
    summary = report.get("summary") or {}
    occurrence_count = sum(
        int(issue.get("occurrence_count") or 1)
        for issue in report.get("issues") or []
    )
    return (
        int(summary.get("fail_count") or 0),
        int(summary.get("warn_count") or 0),
        occurrence_count,
    )


def _word_spans(text: str) -> list[re.Match[str]]:
    return list(re.finditer(r"[^\W_]+", str(text or ""), re.UNICODE))


def _replace_token(text: str, index: int, actual: str, expected: str) -> str | None:
    spans = _word_spans(text)
    if 0 <= index < len(spans):
        span = spans[index]
        if span.group(0) == actual:
            return text[:span.start()] + expected + text[span.end():]
    exact = [span for span in spans if span.group(0) == actual]
    if len(exact) == 1:
        span = exact[0]
        return text[:span.start()] + expected + text[span.end():]
    return None


def _verifier_result(
    verifier: LexicalVerifier | TimingVerifier | None,
    context: Mapping[str, Any],
) -> tuple[bool, float, str, Mapping[str, Any]]:
    if verifier is None:
        return False, 0.0, "independent_verifier_unavailable", {}
    try:
        raw = verifier(context)
    except Exception as exc:  # a provider failure must abstain, never approve
        return False, 0.0, f"verifier_failed:{type(exc).__name__}", {}
    if isinstance(raw, Mapping):
        accepted = bool(raw.get("accepted"))
        try:
            confidence = float(raw.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        return (
            accepted, max(0.0, min(1.0, confidence)),
            str(raw.get("reason") or ""), dict(raw),
        )
    if isinstance(raw, bool):
        return raw, 1.0 if raw else 0.0, "boolean_verifier", {}
    try:
        confidence = max(0.0, min(1.0, float(raw)))
    except (TypeError, ValueError):
        return False, 0.0, "invalid_verifier_result", {}
    return confidence > 0.0, confidence, "numeric_verifier", {}


def _preflight(manifest: Mapping[str, Any], *, quality: Mapping[str, Any] | None) -> dict:
    return build_delivery_preflight(
        metadata=manifest.get("metadata") or {},
        segments=manifest.get("segments") or [],
        approved_lyrics=manifest.get("approved_lyrics"),
        reference_trusted=bool(manifest.get("reference_trusted", False)),
        asset=manifest.get("asset") or {},
        quality=quality,
        reference_health=(
            manifest.get("reference_health")
            if isinstance(manifest.get("reference_health"), Mapping) else None
        ),
        acoustic_findings=(
            manifest.get("acoustic_findings")
            if isinstance(manifest.get("acoustic_findings"), list) else None
        ),
        fps=float(manifest.get("fps", 30.0)),
    )


def _patch_metadata(
    trial: MutableMapping[str, Any], issue: Mapping[str, Any]
) -> tuple[bool, dict[str, Any], str]:
    fields = {
        "METADATA_TITLE_MISMATCH": "rendered_title",
        "METADATA_ARTIST_MISMATCH": "rendered_artist",
        "METADATA_VERSION_MISMATCH": "rendered_version",
    }
    field = fields.get(str(issue.get("code")))
    if not field:
        return False, {}, "unsupported_metadata_issue"
    asset = trial.setdefault("asset", {})
    before = asset.get(field)
    after = issue.get("expected")
    if not after or before == after:
        return False, {}, "no_metadata_change"
    asset[field] = after
    return True, {"path": f"asset.{field}", "before": before, "after": after}, "metadata_is_authoritative"


def _patch_lyric_occurrence(
    trial: MutableMapping[str, Any],
    issue: Mapping[str, Any],
    evidence: Mapping[str, Any],
) -> tuple[bool, dict[str, Any], str]:
    segments = trial.get("segments") or []
    try:
        segment_index = int(evidence.get("segment_index"))
        token_index = int(evidence.get("token_index"))
        segment = segments[segment_index]
    except (TypeError, ValueError, IndexError):
        return False, {}, "invalid_segment_evidence"
    key = "text" if segment.get("text") is not None else "t"
    before = str(segment.get(key) or "")
    after = _replace_token(
        before, token_index, str(issue.get("actual") or ""),
        str(issue.get("expected") or ""),
    )
    if after is None or after == before:
        return False, {}, "token_location_ambiguous"
    segment[key] = after
    return True, {
        "path": f"segments[{segment_index}].{key}",
        "before": before,
        "after": after,
        "token_index": token_index,
    }, "bounded_token_replacement"


def _patch_timeline(
    trial: MutableMapping[str, Any],
    issue: Mapping[str, Any],
    evidence: Mapping[str, Any],
    policy: RepairPolicy,
    verified: Mapping[str, Any] | None = None,
) -> tuple[bool, dict[str, Any], str]:
    segments = trial.get("segments") or []
    code = str(issue.get("code") or "")
    if code == "LYRIC_OUTSIDE_ASSET":
        try:
            index = int(evidence["segment_index"])
            duration = float(evidence["duration"])
            segment = segments[index]
            start = float(segment.get("start", segment.get("s", 0)))
        except (KeyError, TypeError, ValueError, IndexError):
            return False, {}, "invalid_duration_evidence"
        if start >= duration - policy.min_segment_duration_s:
            return False, {}, "segment_starts_outside_asset"
        key = "end" if segment.get("end") is not None else "e"
        before = segment.get(key)
        segment[key] = duration
        return True, {
            "path": f"segments[{index}].{key}", "before": before, "after": duration,
        }, "clamped_to_media_duration"
    if code == "LYRIC_OVERLAP":
        try:
            previous_index = int(evidence["previous_segment_index"])
            current_start = float(evidence["start"])
            overlap = float(evidence["overlap_s"])
            previous = segments[previous_index]
            previous_start = float(previous.get("start", previous.get("s", 0)))
        except (KeyError, TypeError, ValueError, IndexError):
            return False, {}, "invalid_overlap_evidence"
        if overlap > policy.max_safe_overlap_s:
            try:
                current_start = float((verified or {})["previous_end"])
            except (KeyError, TypeError, ValueError):
                return False, {}, "overlap_requires_timing_verifier"
        if current_start - previous_start < policy.min_segment_duration_s:
            return False, {}, "clamp_would_collapse_previous_segment"
        key = "end" if previous.get("end") is not None else "e"
        before = previous.get(key)
        previous[key] = current_start
        return True, {
            "path": f"segments[{previous_index}].{key}",
            "before": before, "after": current_start,
        }, "small_overlap_clamp"
    if code == "PREMATURE_LYRIC_END":
        try:
            index = int(evidence["segment_index"])
            segment = segments[index]
            proposed_end = float(evidence["proposed_end"])
            current_end = float(segment.get("end", segment.get("e")))
            current_start = float(segment.get("start", segment.get("s", 0)))
        except (KeyError, TypeError, ValueError, IndexError):
            return False, {}, "invalid_endpoint_consensus_evidence"
        duration = float((trial.get("asset") or {}).get("duration") or float("inf"))
        next_start = None
        if index + 1 < len(segments):
            try:
                next_start = float(segments[index + 1].get(
                    "start", segments[index + 1].get("s")
                ))
            except (TypeError, ValueError):
                next_start = None
        if (
            proposed_end - current_end < policy.min_segment_duration_s
            or proposed_end <= current_start
            or proposed_end > duration + 0.05
            or (next_start is not None and proposed_end > next_start + 0.001)
        ):
            return False, {}, "verified_endpoint_failed_timeline_bounds"
        key = "end" if segment.get("end") is not None else "e"
        segment[key] = proposed_end
        return True, {
            "path": f"segments[{index}].{key}",
            "before": current_end,
            "after": proposed_end,
        }, "stem_mix_word_clock_endpoint_consensus"
    if code == "INVALID_LYRIC_RANGE":
        try:
            index = int(evidence["segment_index"])
            segment = segments[index]
            proposed_start = float((verified or {}).get(
                "start", segment.get("start", segment.get("s", 0))
            ))
            proposed_end = float((verified or {})["end"])
        except (KeyError, TypeError, ValueError, IndexError):
            return False, {}, "endpoint_verifier_did_not_supply_valid_range"
        duration = float((trial.get("asset") or {}).get("duration") or float("inf"))
        if (
            proposed_start < 0
            or proposed_end - proposed_start < policy.min_segment_duration_s
            or proposed_end > duration + 0.05
        ):
            return False, {}, "verified_range_failed_invariants"
        start_key = "start" if segment.get("start") is not None else "s"
        end_key = "end" if segment.get("end") is not None else "e"
        before = {"start": segment.get(start_key), "end": segment.get(end_key)}
        segment[start_key], segment[end_key] = proposed_start, proposed_end
        return True, {
            "path": f"segments[{index}]",
            "before": before,
            "after": {"start": proposed_start, "end": proposed_end},
        }, "independently_verified_range"
    return False, {}, "timing_requires_independent_endpoint"


def repair_delivery_manifest(
    manifest: Mapping[str, Any],
    *,
    lexical_verifier: LexicalVerifier | None = None,
    timing_verifier: TimingVerifier | None = None,
    policy: RepairPolicy | None = None,
) -> dict[str, Any]:
    """Repair a delivery manifest transactionally and return a full audit."""
    rules = policy or RepairPolicy()
    original = deepcopy(dict(manifest))
    current = deepcopy(original)
    upstream_quality = original.get("quality") if isinstance(original.get("quality"), Mapping) else None
    before_report = _preflight(current, quality=upstream_quality)
    current_report = before_report
    actions: list[dict[str, Any]] = []
    changed_domains: set[str] = set()

    for issue in before_report.get("issues") or []:
        code = str(issue.get("code") or "")
        evidences = issue.get("evidence") or [{}]
        if code.startswith("METADATA_"):
            evidences = [evidences[0] if evidences else {}]
        for occurrence_index, evidence in enumerate(evidences):
            context = {
                "issue": issue,
                "evidence": evidence,
                "manifest": current,
                "policy": rules.__dict__,
            }
            eligible = False
            confidence = float(issue.get("confidence") or 0.0)
            reason = "not_auto_fixable"
            domain = "unknown"
            verification: Mapping[str, Any] = {}
            if code.startswith("METADATA_"):
                eligible, reason, domain = True, "metadata_is_authoritative", "metadata"
            elif code == "LYRIC_ORTHOGRAPHY_MISMATCH":
                eligible, reason, domain = bool(original.get("reference_trusted")), "trusted_orthography", "text"
            elif code == "LYRIC_TOKEN_TYPO":
                domain = "text"
                match = float((evidence or {}).get("reference_match_confidence") or 0.0)
                if original.get("reference_trusted") and match >= rules.trusted_typo_min_match:
                    eligible, confidence, reason = True, min(confidence, match), "trusted_near_exact_reference"
                else:
                    accepted, verify_confidence, verify_reason, verification = _verifier_result(
                        lexical_verifier, context
                    )
                    eligible = accepted and verify_confidence >= rules.verifier_min_confidence
                    confidence, reason = verify_confidence, verify_reason or "lexical_verifier"
            elif code in {
                "LYRIC_OUTSIDE_ASSET", "LYRIC_OVERLAP", "INVALID_LYRIC_RANGE",
                "PREMATURE_LYRIC_END",
            }:
                domain = "timing"
                overlap = float((evidence or {}).get("overlap_s") or 0.0)
                is_live = bool(original.get("is_live")) or str(
                    original.get("performance_type") or ""
                ).lower() == "live"
                deterministic_outside = (
                    code == "LYRIC_OUTSIDE_ASSET" and rules.auto_outside_asset
                )
                experimental_overlap = (
                    code == "LYRIC_OVERLAP" and rules.auto_small_overlap
                    and overlap <= rules.max_safe_overlap_s
                )
                certified_endpoint = bool((evidence or {}).get("certified_for_shadow"))
                endpoint_score = float((evidence or {}).get("consensus_score") or 0.0)
                verified_endpoint = (
                    code == "PREMATURE_LYRIC_END"
                    and rules.auto_verified_endpoints
                    and certified_endpoint
                    and endpoint_score >= rules.verified_endpoint_min_score
                )
                if not is_live and (
                    deterministic_outside or experimental_overlap or verified_endpoint
                ):
                    eligible, reason = True, "bounded_timeline_invariant"
                else:
                    accepted, verify_confidence, verify_reason, verification = _verifier_result(
                        timing_verifier, context
                    )
                    eligible = accepted and verify_confidence >= rules.verifier_min_confidence
                    confidence, reason = verify_confidence, verify_reason or "timing_verifier"

            action_base = {
                "issue_id": issue.get("issue_id"),
                "occurrence_index": occurrence_index,
                "code": code,
                "domain": domain,
                "severity": issue.get("severity"),
                "summary": issue.get("summary"),
                "seek_seconds": (
                    (issue.get("seconds") or [None])[occurrence_index]
                    if occurrence_index < len(issue.get("seconds") or []) else None
                ),
                "timecode": (
                    (issue.get("timecodes") or [None])[occurrence_index]
                    if occurrence_index < len(issue.get("timecodes") or []) else None
                ),
                "actual": issue.get("actual"),
                "expected": issue.get("expected"),
                "confidence": round(confidence, 3),
                "reason": reason,
            }
            action_base["action_id"] = _stable_id(action_base)
            if not eligible:
                status = "ESCALATED" if code.startswith("REFERENCE_") else "PROPOSED"
                actions.append({**action_base, "status": status, "patch": None})
                continue

            trial = deepcopy(current)
            if domain == "metadata":
                patched, patch, patch_reason = _patch_metadata(trial, issue)
            elif domain == "text":
                patched, patch, patch_reason = _patch_lyric_occurrence(trial, issue, evidence)
            else:
                patched, patch, patch_reason = _patch_timeline(
                    trial, issue, evidence, rules, verification
                )
            if not patched:
                actions.append({
                    **action_base, "status": "PROPOSED", "patch": patch or None,
                    "reason": patch_reason,
                })
                continue

            # The upstream Quality v6 result belongs to the old segment hash.
            # Compare deterministic delivery findings now; request a fresh
            # quality analysis after accepting text/timing changes.
            quality_is_current = not changed_domains.intersection({"text", "timing"}) and domain == "metadata"
            comparison_quality = upstream_quality if quality_is_current else None
            trial_report = _preflight(trial, quality=comparison_quality)
            baseline_report = _preflight(current, quality=comparison_quality)
            if _risk_vector(trial_report) < _risk_vector(baseline_report):
                current = trial
                current_report = trial_report
                changed_domains.add(domain)
                actions.append({
                    **action_base, "status": "APPLIED", "patch": patch,
                    "reason": patch_reason,
                    "risk_before": _risk_vector(baseline_report),
                    "risk_after": _risk_vector(trial_report),
                })
            else:
                actions.append({
                    **action_base, "status": "REJECTED_BY_REGRESSION_GUARD",
                    "patch": patch, "reason": "fresh_preflight_did_not_improve",
                    "risk_before": _risk_vector(baseline_report),
                    "risk_after": _risk_vector(trial_report),
                })

    applied = [item for item in actions if item["status"] == "APPLIED"]
    proposed = [item for item in actions if item["status"] == "PROPOSED"]
    escalated = [item for item in actions if item["status"] == "ESCALATED"]
    requirements = []
    if changed_domains.intersection({"text", "timing"}):
        requirements.append("rerun_transcription_quality_for_new_segment_revision")
    if changed_domains:
        requirements.append("render_fresh_preview")
    if proposed:
        requirements.append("review_or_verify_remaining_proposals")
    if escalated:
        requirements.append("reprocess_or_review_blocking_findings")
    editor_items = [
        {
            "id": item["action_id"],
            "status": item["status"],
            "domain": item["domain"],
            "severity": item.get("severity"),
            "label": item.get("summary"),
            "seek_seconds": item.get("seek_seconds"),
            "timecode": item.get("timecode"),
            "current": item.get("actual"),
            "proposed": item.get("expected"),
            "confidence": item.get("confidence"),
            "reason": item.get("reason"),
        }
        for item in actions
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "observe",
        "status": "REPAIRED" if applied else "NO_SAFE_REPAIR",
        "manifest": current,
        "before_preflight": before_report,
        "after_preflight": current_report,
        "summary": {
            "applied_count": len(applied),
            "proposed_count": len(proposed),
            "escalated_count": len(escalated),
            "rejected_count": sum(
                item["status"] == "REJECTED_BY_REGRESSION_GUARD" for item in actions
            ),
            "changed_domains": sorted(changed_domains),
            "risk_before": _risk_vector(before_report),
            "risk_after": _risk_vector(current_report),
        },
        "actions": actions,
        "editor_review": {
            "kind": "delivery_repair_review",
            "review_only": True,
            "items": editor_items,
        },
        "requirements_before_delivery": requirements,
        "after_preflight_provisional": bool(
            changed_domains.intersection({"text", "timing"})
        ),
    }
