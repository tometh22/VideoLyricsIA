"""Line-end timing derived from stable vocal pitch.

The review selector never mutates lyrics.  The automatic tail helper is more
conservative: it may only extend a machine-timed line through a contiguous,
energy-backed pitch sustain and never touches an operator-locked line.
Neither path uses catalogue, UMG or competitor timing at inference time.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np


def automatic_tail_enabled() -> bool:
    """Keep automatic mutation off until operator-gold calibration passes."""
    return os.environ.get(
        "STABLE_PITCH_TAIL_ENABLED", "0",
    ).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class AcousticTrack:
    frame_seconds: float
    times: np.ndarray
    rms: np.ndarray
    active: np.ndarray
    f0: np.ndarray
    voiced_probability: np.ndarray
    pitched: np.ndarray
    energy_threshold: float


def load_acoustic_track(stem_path: Path) -> AcousticTrack:
    """Extract the bounded pitch representation used by the runtime selector."""

    import librosa

    sample_rate = 16000
    hop_length = 800
    waveform, _ = librosa.load(str(stem_path), sr=sample_rate, mono=True)
    rms = librosa.feature.rms(
        y=waveform, frame_length=hop_length * 2, hop_length=hop_length,
    )[0]
    threshold = max(0.12 * float(np.percentile(rms, 95)), 1e-4)
    try:
        f0, voiced_flag, voiced_probability = librosa.pyin(
            waveform, fmin=65, fmax=1000, sr=sample_rate,
            frame_length=2048, hop_length=hop_length,
        )
    except Exception:
        f0 = np.full(len(rms), np.nan)
        voiced_flag = np.zeros(len(rms), dtype=bool)
        voiced_probability = np.zeros(len(rms), dtype=np.float32)
    count = min(len(rms), len(f0), len(voiced_probability), len(voiced_flag))
    rms = np.asarray(rms[:count], dtype=np.float32)
    f0 = np.asarray(f0[:count], dtype=np.float32)
    voiced_probability = np.nan_to_num(
        np.asarray(voiced_probability[:count], dtype=np.float32), nan=0.0,
    )
    active = rms > threshold
    pitched = (
        active & np.asarray(voiced_flag[:count], dtype=bool)
        & np.isfinite(f0) & (voiced_probability >= 0.35)
    )
    return AcousticTrack(
        frame_seconds=hop_length / sample_rate,
        times=np.arange(count, dtype=np.float32) * (hop_length / sample_rate),
        rms=rms, active=active, f0=f0,
        voiced_probability=voiced_probability, pitched=pitched,
        energy_threshold=threshold,
    )


def _track_slice(track: AcousticTrack, start: float, end: float) -> slice:
    left = max(0, int(math.floor(start / track.frame_seconds)))
    right = min(
        len(track.times), int(math.ceil(end / track.frame_seconds)) + 1,
    )
    return slice(left, max(left, right))


def _pitch_summary(track: AcousticTrack, start: float, end: float) -> dict[str, Any]:
    selection = _track_slice(track, start, end)
    pitched = track.pitched[selection]
    f0 = track.f0[selection][pitched]
    fraction = float(np.mean(pitched)) if len(pitched) else 0.0
    median_step = None
    if len(f0) >= 3:
        cents = 1200.0 * np.diff(np.log2(np.maximum(f0, 1e-6)))
        median_step = float(np.median(np.abs(cents)))
    return {
        "pitched_fraction": fraction,
        "median_pitch_step_cents": median_step,
        "stable_pitch": bool(
            fraction >= 0.35 and median_step is not None and median_step <= 150.0
        ),
    }


def _symmetric_phrase_endpoint(
    track: AcousticTrack, start: float, limit: float,
) -> tuple[float | None, dict[str, Any]]:
    if limit <= start + track.frame_seconds:
        return None, {"reason": "empty_symmetric_window"}
    selection = _track_slice(track, start, limit)
    pitched_indices = np.flatnonzero(track.pitched[selection])
    if len(pitched_indices) < 2:
        return None, {"reason": "no_pitch_in_symmetric_window"}
    left = selection.start or 0
    last_pitch = left + int(pitched_indices[-1])
    max_gap_frames = max(1, int(round(0.25 / track.frame_seconds)))
    run_start = last_pitch
    quiet = 0
    for index in range(last_pitch - 1, left - 1, -1):
        if track.pitched[index]:
            run_start = index
            quiet = 0
        else:
            quiet += 1
            if quiet >= max_gap_frames:
                break
    if int(np.sum(track.pitched[run_start:last_pitch + 1])) < 2:
        return None, {"reason": "pitch_run_too_short"}
    endpoint = min(limit, float(track.times[last_pitch] + track.frame_seconds))
    summary = _pitch_summary(track, float(track.times[run_start]), endpoint)
    if not summary["stable_pitch"]:
        return None, {"reason": "symmetric_pitch_run_not_stable", **summary}
    return endpoint, {
        "reason": "selected_symmetric_stable_pitch_end", **summary,
    }


@dataclass(frozen=True)
class TimingReviewPolicy:
    # Endpoint benchmark 2026-09-05, operator gold e926 rev51 (38 locked):
    # raw acoustic endpoint 3/17 within ±150ms, MAE .6444s; subtracting 100ms
    # 2/17, MAE .7199s. Keep the proposal at the measured endpoint. This is
    # observation/reviewer guidance only; automatic timing remains disabled.
    perceptual_lead_s: float = 0.0
    minimum_visible_delta_s: float = 0.15
    maximum_visible_delta_s: float = 6.0
    next_line_guard_s: float = 0.02
    fixed_padding_s: float = 0.25
    fixed_padding_tolerance_s: float = 0.075
    maximum_suggestions: int = 64
    policy_version: str = "t4-human-suggestion-v1"


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _last_word_end(segment: Mapping[str, Any]) -> float | None:
    provider = segment.get("provider_evidence")
    words = provider.get("words") if isinstance(provider, Mapping) else None
    if not isinstance(words, list):
        words = segment.get("words")
    ends = [
        value for word in (words or []) if isinstance(word, Mapping)
        if (value := _number(word.get("end"))) is not None
    ]
    return max(ends) if ends else None


def _stable_tail_endpoint(
    track: AcousticTrack,
    *,
    word_end: float,
    limit: float,
    maximum_pitch_distance_cents: float = 200.0,
    maximum_gap_s: float = 0.15,
) -> float | None:
    """Find the contiguous stable-pitch run attached to ``word_end``."""
    if not len(track.times) or limit <= word_end:
        return None
    anchor = _track_slice(track, max(0.0, word_end - 0.20), word_end + 0.25)
    anchor_indices = np.flatnonzero(track.pitched[anchor])
    if not len(anchor_indices):
        return None
    anchor_left = anchor.start or 0
    absolute_anchor_indices = anchor_left + anchor_indices
    anchor_f0 = track.f0[absolute_anchor_indices]
    anchor_pitch = float(np.median(anchor_f0[np.isfinite(anchor_f0)]))
    if not math.isfinite(anchor_pitch) or anchor_pitch <= 0:
        return None

    start_index = max(0, int(math.floor(word_end / track.frame_seconds)))
    stop_index = min(
        len(track.times), int(math.ceil(limit / track.frame_seconds)) + 1,
    )
    allowed_gap_frames = max(1, int(math.ceil(
        maximum_gap_s / track.frame_seconds,
    )))
    last_stable = None
    gap_frames = 0
    stable_frames = 0
    for index in range(start_index, stop_index):
        f0 = float(track.f0[index])
        within_pitch = bool(
            track.pitched[index]
            and math.isfinite(f0)
            and f0 > 0
            and abs(1200.0 * math.log2(f0 / anchor_pitch))
            <= maximum_pitch_distance_cents
        )
        if within_pitch:
            last_stable = index
            stable_frames += 1
            gap_frames = 0
            continue
        gap_frames += 1
        if gap_frames > allowed_gap_frames:
            break
    if last_stable is None or stable_frames < 2:
        return None
    endpoint = min(
        limit, float(track.times[last_stable] + track.frame_seconds),
    )
    return endpoint if endpoint > word_end else None


def extend_line_ends_to_stable_pitch(
    segments: Sequence[dict[str, Any]],
    track: AcousticTrack,
    *,
    next_line_guard_s: float = 0.02,
    maximum_tail_s: float = 6.0,
    minimum_extension_s: float = 0.10,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Extend only machine-timed lines through an attached vocal sustain."""
    frozen = [dict(item) for item in segments if isinstance(item, dict)]
    output: list[dict[str, Any]] = []
    extended = 0
    abstentions: dict[str, int] = {}

    def abstain(reason: str) -> None:
        abstentions[reason] = abstentions.get(reason, 0) + 1

    for index, segment in enumerate(frozen):
        if segment.get("locked") is True or segment.get("operator_locked") is True:
            output.append(segment)
            abstain("operator_locked")
            continue
        current_end = _number(segment.get("end"))
        word_end = _last_word_end(segment)
        if current_end is None or word_end is None:
            output.append(segment)
            abstain("word_endpoint_unavailable")
            continue
        next_start = (
            _number(frozen[index + 1].get("start"))
            if index + 1 < len(frozen) else None
        )
        track_limit = float(track.times[-1] + track.frame_seconds)
        limit = min(track_limit, word_end + maximum_tail_s)
        if next_start is not None:
            limit = min(limit, next_start - next_line_guard_s)
        endpoint = _stable_tail_endpoint(
            track, word_end=word_end, limit=limit,
        )
        if endpoint is None or endpoint < current_end + minimum_extension_s:
            output.append(segment)
            abstain("no_stable_pitch_extension")
            continue
        updated = dict(segment)
        updated["end"] = round(endpoint, 4)
        updated["stable_pitch_tail_extended"] = True
        updated["stable_pitch_tail_source"] = "pitch_energy_contiguous_v1"
        output.append(updated)
        extended += 1

    report = {
        "schema_version": "stable-pitch-tail-v1",
        "segment_count": len(frozen),
        "extended_count": extended,
        "maximum_pitch_distance_cents": 200.0,
        "maximum_tail_s": maximum_tail_s,
        "next_line_guard_s": next_line_guard_s,
        "abstention_reasons": abstentions,
        "mutated_text": False,
    }
    return (output if extended else list(segments)), report


def _diagnosis(
    *, current_end: float, candidate_end: float, next_start: float | None,
    word_end: float | None, policy: TimingReviewPolicy,
) -> str:
    if candidate_end > current_end:
        return "card_ends_before_stable_vocal_tail"
    if next_start is not None and abs(current_end - next_start) <= 0.06:
        return "display_boundary_inherited_from_next_line"
    if word_end is not None and abs(
        (current_end - word_end) - policy.fixed_padding_s
    ) <= policy.fixed_padding_tolerance_s:
        return "fixed_wrapper_padding"
    return "card_overhang_after_stable_vocal_tail"


def build_timing_review_candidates(
    segments: Sequence[dict[str, Any]],
    track: AcousticTrack,
    *,
    policy: TimingReviewPolicy | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return bounded one-click proposal DTOs without mutating ``segments``."""

    selected_policy = policy or TimingReviewPolicy()
    if not 0 <= selected_policy.perceptual_lead_s <= 0.25:
        raise ValueError("perceptual lead must be between 0 and 250ms")
    candidates: list[dict[str, Any]] = []
    reasons: dict[str, int] = {}

    def abstain(reason: str) -> None:
        reasons[reason] = reasons.get(reason, 0) + 1

    frozen = [dict(item) for item in segments if isinstance(item, dict)]
    for index, segment in enumerate(frozen):
        start = _number(segment.get("start"))
        current_end = _number(segment.get("end"))
        if segment.get("locked") is True or segment.get("operator_locked") is True:
            abstain("operator_locked")
            continue
        if start is None or current_end is None or current_end <= start:
            abstain("invalid_timeline")
            continue
        next_start = (
            _number(frozen[index + 1].get("start"))
            if index + 1 < len(frozen) else None
        )
        search_limit = (
            next_start - selected_policy.next_line_guard_s
            if next_start is not None
            else min(float(track.times[-1]), current_end + selected_policy.maximum_visible_delta_s)
        )
        if search_limit <= start + 0.25:
            abstain("line_window_too_short")
            continue
        acoustic_end, evidence = _symmetric_phrase_endpoint(
            track, max(0.0, start - 0.20), search_limit,
        )
        candidate_end = (
            acoustic_end - selected_policy.perceptual_lead_s
            if acoustic_end is not None else None
        )
        source = "stable_pitch"
        word_end = _last_word_end(segment)

        # A word clock can recover one narrow case that lacks stable pitch,
        # but only inside the same line and within the same maximum delta. It
        # remains medium confidence and can never auto-apply.
        if candidate_end is None and word_end is not None:
            if (
                word_end > current_end + selected_policy.minimum_visible_delta_s
                and word_end <= search_limit + 0.06
            ):
                candidate_end = word_end
                source = "bounded_word_clock_fallback"
        if candidate_end is None:
            abstain(str(evidence.get("reason") or "no_stable_pitch_endpoint"))
            continue
        candidate_end = min(candidate_end, search_limit)
        delta = candidate_end - current_end
        if abs(delta) < selected_policy.minimum_visible_delta_s:
            abstain("delta_below_review_threshold")
            continue
        if abs(delta) > selected_policy.maximum_visible_delta_s:
            abstain("occurrence_jump_veto")
            continue
        if candidate_end <= start:
            abstain("non_positive_candidate")
            continue

        diagnosis = _diagnosis(
            current_end=current_end, candidate_end=candidate_end,
            next_start=next_start, word_end=word_end, policy=selected_policy,
        )
        independent_agreement = bool(
            source == "stable_pitch" and word_end is not None
            and abs(candidate_end - word_end) <= 0.50
        )
        confidence = "high" if independent_agreement else "medium"
        proposed = {**segment, "end": round(candidate_end, 4)}
        window_end = max(current_end, candidate_end)
        candidate_id = f"t4-{index}-{current_end:.3f}-{candidate_end:.3f}"
        candidates.append({
            "kind": "operator_review_candidate",
            "schema": "operator-review-candidate-v1",
            "id": candidate_id,
            "parent_window_id": candidate_id,
            "start": round(start, 4),
            "end": round(window_end, 4),
            "reasons": [diagnosis],
            "current_segments": [dict(segment)],
            "proposed_segments": [proposed],
            "suggestion_type": "timing",
            "confidence": confidence,
            "impact_ms": round(abs(delta) * 1000),
            "current_end": round(current_end, 4),
            "proposed_end": round(candidate_end, 4),
            "raw_acoustic_end": (
                round(acoustic_end, 4) if acoustic_end is not None else None
            ),
            "perceptual_end_offset_s": selected_policy.perceptual_lead_s,
            "preview_start": round(max(0.0, min(current_end, candidate_end) - 1.0), 4),
            "preview_end": round(max(current_end, candidate_end) + 1.0, 4),
            "source_families": (
                ["stable_pitch", "provider_word_clock"]
                if independent_agreement else [source]
            ),
            "selector_policy": selected_policy.policy_version,
            "automatic_apply_allowed": False,
            "reference_data_used": False,
        })

    candidates.sort(key=lambda item: (
        0 if item["confidence"] == "high" else 1,
        -int(item["impact_ms"]), float(item["start"]), str(item["id"]),
    ))
    candidates = candidates[:selected_policy.maximum_suggestions]
    return candidates, {
        "schema_version": "t4-human-suggestion-report-v1",
        "policy": asdict(selected_policy),
        "segment_count": len(frozen),
        "proposal_count": len(candidates),
        "high_confidence_count": sum(
            item["confidence"] == "high" for item in candidates
        ),
        "abstention_reasons": reasons,
        "mutated_segments": False,
        "automatic_apply_allowed": False,
        "reference_data_used": False,
    }
