"""Experimental endpoint proposals from a clock-verified vocal stem.

Follow the final word's spectral envelope instead of requiring a constant F0.
A two-threshold boundary agreement vetoes unstable/reverberant tails. These
are acoustic proposals, not lead-singer or lexical verification. No reference
lyrics or approved endpoints are inputs to inference.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
import hashlib
import json
import math
import os
import re
import unicodedata
from typing import Any, Callable, Mapping, Sequence

import numpy as np


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _last_word_bounds(segment: Mapping[str, Any]) -> tuple[float, float] | None:
    # Current alignment is the timing authority. Frozen provider evidence is
    # the original recognition clock and can lag behind CTC/operator timing.
    # An explicitly present but broken alignment must not silently fall back.
    words = segment.get("words")
    if words is None:
        provider = segment.get("provider_evidence")
        words = provider.get("words") if isinstance(provider, Mapping) else None
    if not isinstance(words, list) or not words:
        return None
    previous_end = -math.inf
    bounds = None
    for word in words:
        if not isinstance(word, Mapping):
            return None
        start, end = _number(word.get("start")), _number(word.get("end"))
        if (
            start is None
            or end is None
            or start < 0
            or end <= start
            or end < previous_end
        ):
            return None
        previous_end = end
        bounds = (start, end)
    return bounds


FEATURE_VERSION = "vocal-continuity-v1"
PERIODICITY_VERSION = "tail-periodicity-v1"
POLICY_VERSION = "vocal-continuity-v2-periodicity"


def continuity_mode() -> str:
    """Explicit mode takes precedence over the original shadow-only flag."""
    configured = os.environ.get("VOCAL_CONTINUITY_MODE")
    if configured is not None:
        value = configured.strip().lower()
        return value if value in {"off", "shadow", "apply"} else "off"
    return (
        "shadow"
        if os.environ.get("VOCAL_CONTINUITY_SHADOW_ENABLED", "0").strip().lower()
        in {"1", "true", "yes", "on"}
        else "off"
    )


def _application_environment_allowed() -> bool:
    # This integration is a staging pilot, not a production rollout. A v6
    # quality signature is NOT used to authorize this experimental post-pass.
    return os.environ.get("ENVIRONMENT", "production").strip().lower() in {
        "staging",
        "test",
        "development",
    }


def _segments_fingerprint(segments: Sequence[Mapping[str, Any]]) -> str:
    return hashlib.sha256(
        json.dumps(
            segments,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


def _protected_document(result: Mapping[str, Any]) -> bool:
    return bool(
        (result.get("approved_text_reuse") or {}).get("used") is True
        or result.get("is_approved")
        or result.get("approved_at")
        or result.get("locked")
        or result.get("operator_locked")
    )


@dataclass(frozen=True)
class VocalTrack:
    times: np.ndarray
    rms: np.ndarray
    spectrum: np.ndarray  # frame x band, unit-normalized square-root mel energy
    frame_seconds: float
    duration_s: float
    clock: dict[str, Any]


@dataclass(frozen=True)
class ContinuityPolicy:
    energy_ratio: float = 0.20
    spectral_similarity: float = 0.65
    maximum_gap_s: float = 0.10
    maximum_boundary_spread_s: float = 0.12
    maximum_extension_s: float = 3.0
    maximum_trim_s: float = 0.6
    minimum_delta_s: float = 0.15
    anchor_lookback_s: float = 0.30
    # Optional conservative calibration gate: corrections are accepted only
    # in this direction. No default automatic mutation is enabled.
    direction: str = "both"


def verify_stem_clock(
    mix: np.ndarray, stem: np.ndarray, sample_rate: int
) -> dict[str, Any]:
    """Require a consistent waveform lag in three separated song regions.

    Same duration alone does not prove clock identity. Correlate active stem
    windows against the original mix, rejecting weak/ambiguous matches and
    drift. Returned offset is stem time minus original time.
    """
    from scipy.signal import correlate

    if sample_rate <= 0 or mix.ndim != 1 or stem.ndim != 1:
        return {"verified": False, "reason": "invalid_waveform"}
    duration_difference = (len(stem) - len(mix)) / sample_rate
    if abs(duration_difference) > 0.25 or min(len(mix), len(stem)) < sample_rate * 9:
        return {"verified": False, "reason": "duration_mismatch_or_short_audio"}
    if not np.all(np.isfinite(mix)) or not np.all(np.isfinite(stem)):
        return {"verified": False, "reason": "nonfinite_waveform"}
    half = sample_rate  # two-second anchors
    radius = int(0.5 * sample_rate)
    count = min(len(mix), len(stem))
    records = []
    for region in range(3):
        left = max(half + radius, int(count * region / 3))
        right = min(count - half - radius, int(count * (region + 1) / 3))
        centers = np.arange(left, right, half)
        if not len(centers):
            continue
        center = int(
            max(centers, key=lambda c: float(np.mean(stem[c - half : c + half] ** 2)))
        )
        target = stem[center - half : center + half].astype(np.float64)
        search = mix[center - half - radius : center + half + radius].astype(np.float64)
        target -= target.mean()
        search -= search.mean()
        corr = correlate(search, target, mode="valid", method="fft")
        energy = search**2
        cumulative = np.r_[0.0, np.cumsum(energy)]
        energy = cumulative[len(target) :] - cumulative[: -len(target)]
        corr = np.abs(corr) / np.sqrt(
            np.maximum(energy * float(target @ target), 1e-24)
        )
        index = int(np.argmax(corr))
        peak = float(corr[index])
        outside = corr.copy()
        outside[
            max(0, index - int(0.02 * sample_rate)) : index
            + int(0.02 * sample_rate)
            + 1
        ] = 0
        second = float(np.max(outside))
        lag = (radius - index) / sample_rate
        records.append(
            {
                "offset_s": lag,
                "correlation": round(peak, 5),
                "peak_ratio": round(peak / max(second, 1e-9), 4),
            }
        )
    good = [
        r
        for r in records
        if r["correlation"] >= 0.10
        and r["peak_ratio"] >= 1.15
        and abs(r["offset_s"]) < 0.49
    ]
    verified = len(good) == 3 and np.ptp([r["offset_s"] for r in good]) <= 0.01
    return {
        "verified": bool(verified),
        "reason": "consistent_waveform_clock"
        if verified
        else "weak_ambiguous_or_drifting_clock",
        "offset_s": float(np.median([r["offset_s"] for r in good]))
        if verified
        else None,
        "duration_difference_s": round(duration_difference, 5),
        "windows": records,
    }


def extract_vocal_track(original: Path, stem: Path) -> VocalTrack:
    import librosa

    sr = 16000
    hop = 320
    mix, _ = librosa.load(str(original), sr=sr, mono=True)
    voice, _ = librosa.load(str(stem), sr=sr, mono=True)
    clock = verify_stem_clock(mix, voice, sr)
    if not clock["verified"]:
        return VocalTrack(
            np.array([]),
            np.array([]),
            np.empty((0, 32)),
            hop / sr,
            len(mix) / sr,
            clock,
        )
    energy = librosa.feature.melspectrogram(
        y=voice,
        sr=sr,
        n_fft=1024,
        hop_length=hop,
        n_mels=32,
        fmin=180,
        fmax=5000,
        power=2,
    )
    rms = np.sqrt(np.maximum(energy.sum(axis=0), 0))
    envelope = np.sqrt(np.maximum(energy.T, 0))
    envelope /= np.maximum(np.linalg.norm(envelope, axis=1, keepdims=True), 1e-12)
    times = np.arange(len(rms)) * hop / sr - clock["offset_s"]
    return VocalTrack(times, rms, envelope, hop / sr, len(mix) / sr, clock)


def _token(value: Any) -> str:
    return re.sub(
        r"[^\w]", "", unicodedata.normalize("NFC", str(value or "")).casefold()
    )


def _endpoint(
    track: VocalTrack,
    anchor: int,
    limit: int,
    reference: np.ndarray,
    level: float,
    ratio: float,
    policy: ContinuityPolicy,
) -> tuple[int | None, str]:
    last = anchor
    for i in range(anchor + 1, limit):
        voiced = (
            track.rms[i] >= level * ratio
            and float(track.spectrum[i] @ reference) >= policy.spectral_similarity
        )
        if voiced:
            last = i
        elif (i - last) * track.frame_seconds > policy.maximum_gap_s + 1e-8:
            return last + 1, "observed_release"
    # A search limit or next line is not evidence that the voice ended there.
    return None, "censored_by_search_limit"


def propose_vocal_endings(
    segments: Sequence[Mapping[str, Any]],
    track: VocalTrack,
    policy: ContinuityPolicy | None = None,
) -> tuple[list[dict], dict]:
    policy = policy or ContinuityPolicy()
    if (
        policy.direction not in {"both", "extend", "trim"}
        or not (0 < policy.energy_ratio < 0.8)
        or not (0 < policy.spectral_similarity <= 1)
        or any(
            not math.isfinite(v) or v < 0
            for v in (
                policy.maximum_gap_s,
                policy.maximum_boundary_spread_s,
                policy.maximum_extension_s,
                policy.maximum_trim_s,
                policy.minimum_delta_s,
                policy.anchor_lookback_s,
            )
        )
    ):
        raise ValueError("invalid_continuity_policy")
    candidates = []
    reasons = {}

    def reject(reason):
        reasons[reason] = reasons.get(reason, 0) + 1

    if not track.clock.get("verified"):
        return [], {
            "policy": asdict(policy),
            "proposal_count": 0,
            "abstention_reasons": {"unverified_stem_clock": len(segments)},
        }
    for i, seg in enumerate(segments):
        if seg.get("locked") or seg.get("operator_locked"):
            reject("operator_locked")
            continue
        start, end = _number(seg.get("start")), _number(seg.get("end"))
        bounds = _last_word_bounds(seg)
        if start is None or end is None or start < 0 or end <= start or bounds is None:
            reject("invalid_or_missing_word_clock")
            continue
        ws, we = bounds
        words = seg.get("words")
        if words is None:
            words = (seg.get("provider_evidence") or {}).get("words")
        final_text = str(seg.get("text") or "").split()
        word_token = _token(words[-1].get("word", words[-1].get("text")))
        if not final_text or not word_token or word_token != _token(final_text[-1]):
            reject("word_text_mismatch")
            continue
        next_start = (
            _number(segments[i + 1].get("start"))
            if i + 1 < len(segments)
            else track.duration_s
        )
        if (
            next_start is None
            or ws < start
            or we > next_start - 0.02
            or ws >= we
            or end > track.duration_s
        ):
            reject("word_clock_outside_line")
            continue
        search_end = min(
            next_start - 0.02, track.duration_s, end + policy.maximum_extension_s
        )
        left = int(np.searchsorted(track.times, max(ws, we - policy.anchor_lookback_s)))
        right = int(np.searchsorted(track.times, min(we, search_end), side="right"))
        if right - left < 3:
            reject("short_anchor")
            continue
        levels = track.rms[left:right]
        # Absolute per-song noise floor prevents fitting an anchor to silence.
        floor = max(float(np.percentile(track.rms, 90)) * 0.025, 1e-6)
        level = float(np.percentile(levels, 75))
        if level < floor:
            reject("inaudible_word_anchor")
            continue
        indices = np.flatnonzero(levels >= level * 0.6) + left
        anchor = int(indices[-1])
        reference = np.median(track.spectrum[indices], axis=0)
        norm = float(np.linalg.norm(reference))
        if norm < 1e-8:
            reject("empty_spectral_anchor")
            continue
        reference /= norm
        if float(track.spectrum[anchor] @ reference) < policy.spectral_similarity:
            reject("inconsistent_word_anchor")
            continue
        limit = int(np.searchsorted(track.times, search_end, side="left"))
        low, reason = _endpoint(
            track, anchor, limit, reference, level, policy.energy_ratio, policy
        )
        high, _ = _endpoint(
            track,
            anchor,
            limit,
            reference,
            level,
            min(0.9, policy.energy_ratio * 1.8),
            policy,
        )
        if low is None or high is None:
            reject(reason if low is None else "censored_by_search_limit")
            continue
        spread = abs(low - high) * track.frame_seconds
        if spread > policy.maximum_boundary_spread_s + 1e-8:
            reject("unstable_release")
            continue
        # Earliest credible release avoids chasing low-level reverberation.
        endpoint = float(track.times[min(low, high)])
        delta = endpoint - end
        if (
            endpoint <= start
            or endpoint > search_end
            or delta > policy.maximum_extension_s
            or delta < -policy.maximum_trim_s
        ):
            reject("correction_out_of_bounds")
            continue
        if abs(delta) < policy.minimum_delta_s:
            reject("already_within_tolerance")
            continue
        if (policy.direction == "extend" and delta < 0) or (
            policy.direction == "trim" and delta > 0
        ):
            reject("direction_gate")
            continue
        candidates.append(
            {
                "segment_index": i,
                "current_end": end,
                "proposed_end": round(endpoint, 4),
                "delta_s": round(delta, 4),
                "boundary_spread_s": round(spread, 4),
                "source": "clock_verified_vocal_continuity",
                "automatic_apply_allowed": False,
                "lead_voice_certified": False,
                "word_clock_source": "current"
                if seg.get("words") is not None
                else "provider",
            }
        )
    return candidates, {
        "policy": asdict(policy),
        "proposal_count": len(candidates),
        "abstention_reasons": reasons,
        "feature_version": FEATURE_VERSION,
        "automatic_apply_allowed": False,
    }


def periodicity_support(
    times: np.ndarray,
    f0: np.ndarray,
    voiced: np.ndarray,
    probability: np.ndarray,
    start: float,
    end: float,
) -> dict:
    """Veto tails with no reliable periodic evidence *inside* added audio.

    Two adjacent 20ms frames are the minimum non-isolated witness, using the
    existing .35 pitch confidence floor. The 64ms margin keeps each centered
    2048-sample analysis window wholly inside the extension: voice before the
    old end or after the proposed end cannot accidentally certify the tail.
    No constant note is required. This is not proof of lead-vocal identity.
    """
    arrays = (times, f0, voiced, probability)
    if (
        any(a.ndim != 1 for a in arrays)
        or len({len(a) for a in arrays}) != 1
        or not len(times)
        or not np.all(np.isfinite(times))
        or np.any(np.diff(times) <= 0)
        or not math.isfinite(start)
        or not math.isfinite(end)
        or end <= start
    ):
        return {"supported": False, "reason": "invalid_periodicity_evidence"}
    interior = (times - 0.064 >= start) & (times + 0.064 <= end)
    reliable = (
        interior
        & voiced
        & np.isfinite(f0)
        & (f0 >= 65)
        & (f0 <= 1000)
        & np.isfinite(probability)
        & (probability >= 0.35)
        & (probability <= 1)
    )
    longest = run = 0
    for i, valid in enumerate(reliable):
        if not valid:
            run = 0
        elif (
            i and reliable[i - 1] and abs(float(times[i] - times[i - 1]) - 0.02) <= 1e-6
        ):
            run += 1
        else:
            run = 1
        longest = max(longest, run)
    supported = longest >= 2
    return {
        "supported": supported,
        "reason": "periodic_evidence_in_extension"
        if supported
        else "no_reliable_periodicity_in_extension",
        "reliable_frames": int(reliable.sum()),
        "longest_run_frames": longest,
        "interior_frames": int(interior.sum()),
        "minimum_probability": 0.35,
        "frame_seconds": 0.02,
        "analysis_window_s": 0.128,
        "version": PERIODICITY_VERSION,
        "lead_voice_certified": False,
    }


def measure_tail_periodicity(
    stem_path: Path, start: float, end: float, clock_offset_s: float
) -> dict:
    """Analyze only a bounded candidate, on the verified original clock."""
    import librosa

    if (
        not all(math.isfinite(v) for v in (start, end, clock_offset_s))
        or start < 0
        or not 0 < end - start <= 3.0001
    ):
        return {"supported": False, "reason": "invalid_periodicity_window"}
    # Context stabilizes pYIN; it is excluded from the evidence mask above.
    stem_start = max(0.0, start - 1.0 + clock_offset_s)
    stem_end = end + 0.5 + clock_offset_s
    waveform, _ = librosa.load(
        str(stem_path),
        sr=16000,
        mono=True,
        offset=stem_start,
        duration=stem_end - stem_start,
    )
    if len(waveform) < 2048:
        return {"supported": False, "reason": "short_periodicity_audio"}
    f0, voiced, probability = librosa.pyin(
        waveform, sr=16000, fmin=65, fmax=1000, frame_length=2048, hop_length=320
    )
    times = stem_start - clock_offset_s + np.arange(len(f0)) * 0.02
    return periodicity_support(times, f0, voiced, probability, start, end)


def screen_vocal_extensions(
    candidates: Sequence[Mapping[str, Any]],
    track: VocalTrack,
    probe: Callable[[float, float], dict],
) -> tuple[list[dict], dict]:
    """Apply the same fail-closed second stage in the worker and offline replay."""
    accepted = []
    reasons = {}
    rejected = []
    for candidate in candidates:
        start = _number(candidate.get("current_end"))
        end = _number(candidate.get("proposed_end"))
        if not track.clock.get("verified"):
            evidence = {"supported": False, "reason": "unverified_stem_clock"}
        elif start is None or end is None or end <= start:
            evidence = {"supported": False, "reason": "extension_required"}
        else:
            try:
                evidence = probe(start, end)
            except Exception as exc:
                evidence = {
                    "supported": False,
                    "reason": "periodicity_extraction_failed",
                    "error_type": type(exc).__name__,
                }
        if evidence.get("supported") is True:
            accepted.append(
                {
                    **candidate,
                    "periodicity_evidence": evidence,
                    "selector_policy": POLICY_VERSION,
                    "automatic_apply_allowed": False,
                }
            )
        else:
            reason = str(evidence.get("reason") or "periodicity_declined")
            reasons[reason] = reasons.get(reason, 0) + 1
            rejected.append(
                {
                    "segment_index": candidate.get("segment_index"),
                    "current_end": start,
                    "proposed_end": end,
                    "evidence": evidence,
                }
            )
    return accepted, {
        "input_candidates": len(candidates),
        "accepted_candidates": len(accepted),
        "abstention_reasons": reasons,
        "rejected_candidates": rejected,
        "policy_version": POLICY_VERSION,
    }


def observe_vocal_continuity(
    result: Mapping[str, Any], original_path: str, stem_path: str | None
) -> dict[str, Any] | None:
    """Opt-in shadow hook: calculate proposals, never change the document.

    The caller supplies its cached separation of this very input. This hook
    does not separate audio, start a provider call, or require fresh review.
    """
    if continuity_mode() == "off":
        return None
    if _protected_document(result):
        return {
            "status": "skipped",
            "reason": "approved_document_reused",
            "automatic_apply_allowed": False,
        }
    if not stem_path:
        return {
            "status": "skipped",
            "reason": "cached_stem_unavailable",
            "automatic_apply_allowed": False,
        }
    try:
        fingerprint = _segments_fingerprint(result.get("segments") or [])
        track = extract_vocal_track(Path(original_path), Path(stem_path))
        # Frozen on calibration before the held-out evaluation. This policy
        # remains observation-only; regression evidence is not deployment approval.
        policy = ContinuityPolicy(
            energy_ratio=0.15,
            spectral_similarity=0.65,
            minimum_delta_s=0.50,
            direction="extend",
        )
        candidates, report = propose_vocal_endings(
            result.get("segments") or [], track, policy
        )
        candidates, screening = screen_vocal_extensions(
            candidates,
            track,
            lambda start, end: measure_tail_periodicity(
                Path(stem_path), start, end, track.clock["offset_s"]
            ),
        )
        return {
            **report,
            "proposal_count": len(candidates),
            "periodicity_screening": screening,
            "status": "shadow",
            "clock": track.clock,
            "audio_duration_s": track.duration_s,
            "source_segments_fingerprint": fingerprint,
            "candidates": candidates,
            "mutated_segments": False,
            "automatic_apply_allowed": False,
        }
    except Exception as exc:
        return {
            "status": "abstained",
            "reason": "acoustic_extraction_failed",
            "error_type": type(exc).__name__,
            "automatic_apply_allowed": False,
        }


def _apply_validated_extensions(
    result: Mapping[str, Any], report: dict
) -> tuple[dict, dict]:
    """Consume only fresh, screened proposals; preserve word/provider evidence."""
    segments = result.get("segments") or []
    if (
        _protected_document(result)
        or report.get("status") != "shadow"
        or not (report.get("clock") or {}).get("verified")
        or report.get("source_segments_fingerprint") != _segments_fingerprint(segments)
    ):
        return dict(result), {
            **report,
            "status": "abstained",
            "reason": "stale_or_protected_result",
            "applied_count": 0,
            "mutated_segments": False,
        }
    duration = _number(report.get("audio_duration_s"))
    if duration is None or duration <= 0:
        return dict(result), {
            **report,
            "status": "abstained",
            "reason": "missing_audio_duration",
            "applied_count": 0,
            "mutated_segments": False,
        }
    output = list(segments)
    applied = []
    rejected = []
    candidates = report.get("candidates") or []
    indices = [c.get("segment_index") for c in candidates]
    for candidate in candidates:
        index = candidate.get("segment_index")
        if (
            type(index) is not int
            or not 0 <= index < len(segments)
            or indices.count(index) != 1
        ):
            rejected.append(
                {"segment_index": index, "reason": "invalid_or_duplicate_index"}
            )
            continue
        segment = segments[index]
        end = _number(segment.get("end"))
        start = _number(segment.get("start"))
        old = _number(candidate.get("current_end"))
        proposed = _number(candidate.get("proposed_end"))
        next_start = (
            _number(segments[index + 1].get("start"))
            if index + 1 < len(segments)
            else duration + 0.02
        )
        evidence = candidate.get("periodicity_evidence") or {}
        spread = _number(candidate.get("boundary_spread_s"))
        if (
            segment.get("locked")
            or segment.get("operator_locked")
            or segment.get("is_approved")
            or segment.get("approved_at")
            or segment.get("vocal_continuity_adjustment")
            or any(v is None for v in (start, end, old, proposed, next_start, spread))
            or not 0 <= start < end
            or abs(end - old) > 1e-6
            or not 0.5 <= proposed - end <= 3.0001
            or proposed > min(duration, next_start - 0.02) + 1e-6
            or not 0 <= spread <= 0.120001
            or candidate.get("selector_policy") != POLICY_VERSION
            or evidence.get("supported") is not True
            or evidence.get("version") != PERIODICITY_VERSION
            or evidence.get("longest_run_frames", 0) < 2
        ):
            rejected.append({"segment_index": index, "reason": "application_guard"})
            continue
        adjustment = {
            "policy_version": POLICY_VERSION,
            "original_end": end,
            "applied_end": proposed,
            "word_timestamps_changed": False,
            "clock_offset_s": report["clock"].get("offset_s"),
            "periodicity_evidence": dict(evidence),
        }
        output[index] = {
            **segment,
            "end": proposed,
            "vocal_continuity_adjustment": adjustment,
        }
        applied.append(
            {"segment_index": index, "original_end": end, "applied_end": proposed}
        )
    updated = {**result, "segments": output} if applied else dict(result)
    return updated, {
        **report,
        "status": "applied" if applied else "unchanged",
        "applied_count": len(applied),
        "applied": applied,
        "application_rejections": rejected,
        "mutated_segments": bool(applied),
        "automatic_apply_allowed": True,
        "result_segments_fingerprint": _segments_fingerprint(output),
    }


def process_vocal_continuity(
    result: dict, original_path: str, stem_path: str | None
) -> dict:
    """Shared staging post-pass for queued and inline transcription paths."""
    mode = continuity_mode()
    if mode == "off":
        return result
    try:
        previous = (result.get("postpass_stats") or {}).get("vocal_continuity") or {}
        if previous.get("status") == "applied" and previous.get(
            "result_segments_fingerprint"
        ) == _segments_fingerprint(result.get("segments") or []):
            return result  # A second quality measurement must not extend twice.
        if mode == "apply" and not _application_environment_allowed():
            report = {
                "status": "skipped",
                "reason": "application_environment_not_allowed",
                "mutated_segments": False,
                "applied_count": 0,
                "automatic_apply_allowed": False,
            }
        else:
            report = observe_vocal_continuity(result, original_path, stem_path)
            if mode == "apply" and report and report.get("status") == "shadow":
                result, report = _apply_validated_extensions(result, report)
        if report is None:
            return result
    except Exception as exc:
        report = {
            "status": "abstained",
            "reason": "continuity_postpass_failed",
            "error_type": type(exc).__name__,
            "mutated_segments": False,
            "applied_count": 0,
            "automatic_apply_allowed": False,
        }
    return {
        **result,
        "postpass_stats": {
            **(result.get("postpass_stats") or {}),
            "vocal_continuity": {**report, "mode": mode},
        },
    }
