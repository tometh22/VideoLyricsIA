"""Bounded acoustic evidence for the local model; no editor mutations.

Activity suppression is an experimental negative-evidence policy, not proof of
silence or a singing/speech accuracy certificate. Endpoint proposals reuse the
existing screened extension-only policy; a CTC endpoint alone never trims text.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path

from reviewer_shadow import source_binding, validate_snapshot
from reviewer_shadow_audio import file_sha, private_write
from reviewer_singing_activity import REVISION
from shadow_reference_import import digest

CHECKPOINT_SHA256 = "f09cd6a55c83c13640622ce67480443ce282e3124dd22fccee8a898bb2ba9dee"


@dataclass(frozen=True)
class ActivityPolicy:
    maximum_probability: float = .05
    maximum_mean_probability: float = .02
    minimum_seconds: float = .5
    maximum_seconds: float = 30.
    version: str = "genly-low-singing-activity-v1-experimental"


def classify_nonvocal_window(activity: dict, window: dict, *, audio_sha256: str,
                            audio_duration: float, policy: ActivityPolicy | None = None) -> dict:
    """Validate the complete frame span before using independent ASR-veto evidence.

No lyric text, editor segment boundaries, or recognition outputs are inputs.
Missing frames/context cause abstention. A low learned activity score remains
fallible, especially for speech, backing vocals and unusual singing.
"""
    policy = policy or ActivityPolicy()
    if not (0 < policy.maximum_mean_probability <= policy.maximum_probability <= .05
            and .5 <= policy.minimum_seconds <= policy.maximum_seconds <= 30):
        raise ValueError("invalid_activity_policy")
    result = {"status": "unavailable", "suppression_allowed": False, "audio_sha256": audio_sha256,
              "window": deepcopy(window), "policy": asdict(policy), "correctness_certified": False,
              "catalog_accuracy_validated": False, "editor_lyrics_used": False,
              "spoken_voice_absence_certified": False, "automatic_apply_allowed": False}
    def decline(reason):
        return {**result, "reason": reason}
    if (activity.get("audio_sha256") != audio_sha256 or activity.get("clock") != "original_mix_decoded"
            or activity.get("model") != "CPJKU/veracity/model_log_0mean" or activity.get("revision") != REVISION
            or activity.get("checkpoint_sha256") != CHECKPOINT_SHA256):
        return decline("activity_source_or_model_mismatch")
    try:
        start, end = float(window["start"]), float(window["end"])
        a, b = float(activity["window"]["start"]), float(activity["window"]["end"])
        hop, receptive = float(activity["frame_step_seconds"]), float(activity["receptive_field_seconds"])
        if (not all(math.isfinite(v) for v in (start, end, a, b, hop, receptive, audio_duration))
                or not 0 <= start < end <= audio_duration or not policy.minimum_seconds <= end-start <= policy.maximum_seconds
                or abs(hop - 315/22050) > 1e-8 or abs(receptive - 115/70) > 1e-8):
            return decline("invalid_activity_clock_or_window")
        # At a real file boundary the unavailable future/past is physical EOF,
        # unlike artificial crop padding. Keep this distinction in the receipt.
        margin = receptive / 2
        if a > max(0., start-margin) + hop or b < min(audio_duration, end+margin) - hop:
            return decline("activity_context_insufficient")
        frames = activity.get("frames")
        if not isinstance(frames, list) or not frames or len(frames) > 100000:
            return decline("activity_frames_missing")
        selected = []
        previous = None
        for row in frames:
            time, probability = float(row["time"]), float(row["probability"])
            if not math.isfinite(time) or not math.isfinite(probability) or not 0 <= probability <= 1:
                return decline("activity_frames_invalid")
            if previous is not None and abs((time-previous)-hop) > hop * .02:
                return decline("activity_frame_gap_or_clock_drift")
            previous = time
            if start <= time < end:
                selected.append((time, probability))
        if not selected or selected[0][0] > start+hop+.0001 or selected[-1][0] < end-hop-.0001:
            return decline("activity_target_not_covered")
        if abs(float(frames[0]["time"])-a) > hop * .02 or not b-hop-.0001 <= float(frames[-1]["time"]) <= b+hop:
            return decline("activity_context_frames_missing")
        probabilities = [p for _, p in selected]
        maximum, mean = max(probabilities), sum(probabilities)/len(probabilities)
    except (KeyError, TypeError, ValueError, OverflowError):
        return decline("activity_receipt_invalid")
    supported = maximum <= policy.maximum_probability and mean <= policy.maximum_mean_probability
    return {**result, "status": "nonvocal_supported" if supported else "vocal_or_uncertain",
            "suppression_allowed": supported, "reason": "low_activity_across_entire_window" if supported else "activity_not_consistently_low",
            "maximum_probability": maximum, "mean_probability": mean, "frames_used": len(selected),
            "physical_file_edge": start == 0 or abs(end-audio_duration) < .001,
            "activity_evidence_sha256": digest(activity)}


def activity_for_window(audio: Path, vendor: Path, window: dict, *, audio_duration: float,
                        cache_dir: Path, activity_runner=None) -> dict:
    """Explicit CPU inference with padded context and private immutable receipts."""
    from reviewer_singing_activity import run_activity
    audio_sha256 = file_sha(audio)
    checkpoint = vendor / "dataset/jamendo/models/model_log_0mean/model.pt"
    if file_sha(checkpoint) != CHECKPOINT_SHA256:
        raise ValueError("activity_checkpoint_mismatch")
    expanded = {"start": max(0., float(window["start"])-1.),
                "end": min(audio_duration, float(window["end"])+1.)}
    if not 0 < expanded["end"]-expanded["start"] <= 32:
        raise ValueError("activity_window_budget_exceeded")
    identity = digest([audio_sha256, REVISION, CHECKPOINT_SHA256, expanded])
    cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = cache_dir / (identity + ".json")
    if path.exists():
        activity = json.loads(path.read_text())
    else:
        activity = (activity_runner or run_activity)(vendor, audio, expanded)
        private_write(path, activity)
    return classify_nonvocal_window(activity, window, audio_sha256=audio_sha256, audio_duration=audio_duration)


def endpoint_extension_candidates(snapshot: dict, audio: Path, stem: Path | None, *,
                                  candidate_segments: list[dict] | None = None, observer=None) -> dict:
    """Use the established continuity/periodicity policy on unchanged text only.

Default observer honors VOCAL_CONTINUITY_MODE; use shadow in the local worker.
The existing pure application validator is run against an isolated copy only
to reuse its exact clock, spread, direction, duration, and periodicity guards.
Its simulated mutations are never returned as an editor application receipt.
"""
    from vocal_continuity import observe_vocal_continuity, _apply_validated_extensions
    validate_snapshot(snapshot)
    result = {"source": source_binding(snapshot), "status": "unavailable", "candidates": [],
              "automatic_apply_allowed": False, "mutated_segments": False,
              "policy": "vocal-continuity-v2-periodicity", "ctc_endpoint_replacement": False}
    if len(snapshot["segments"]) > 256:
        return {**result, "reason": "endpoint_line_budget_exceeded"}
    if file_sha(audio) != snapshot["audio_sha256"]:
        raise ValueError("audio_identity_changed")
    if stem is None or not Path(stem).is_file():
        return {**result, "reason": "verified_stem_unavailable"}
    if snapshot.get("approved_at") or snapshot.get("status") in {"done", "lyrics_approved"}:
        return {**result, "reason": "approved_song_preserved"}
    baseline = deepcopy(snapshot["segments"])
    candidate_segments = candidate_segments if candidate_segments is not None else baseline
    if len(candidate_segments) != len(baseline):
        raise ValueError("candidate_line_correspondence_required")
    view = lambda row: tuple(row.get(k) for k in ("text", "start", "end"))
    originals = {view(row) for row in snapshot.get("original_segments", [])}
    for index, row in enumerate(baseline):
        if view(row) not in originals or candidate_segments[index].get("text") != row.get("text"):
            row["operator_locked"] = True
    source = {"segments": baseline}
    report = (observer or observe_vocal_continuity)(source, str(audio), str(stem))
    if not report:
        return {**result, "reason": "continuity_observer_disabled"}
    _, validated = _apply_validated_extensions(source, report)
    accepted = {row["segment_index"]: row for row in validated.get("applied", [])}
    candidates = []
    stem_sha256 = file_sha(stem)
    for proposal in report.get("candidates", []):
        index = proposal.get("segment_index")
        if index not in accepted:
            continue
        candidates.append({**deepcopy(proposal), "line_index": index,
                           "source": source_binding(snapshot), "audio_sha256": snapshot["audio_sha256"],
                           "stem_sha256": stem_sha256, "clock": deepcopy(report.get("clock")),
                           "automatic_apply_allowed": False, "text_verified": False,
                           "word_timestamps_changed": False})
    ready = report.get("status") == "shadow" and (report.get("clock") or {}).get("verified")
    return {**result, "status": "proposals" if candidates else "unchanged" if ready else "unavailable", "candidates": candidates,
            "clock": report.get("clock"), "observer_status": report.get("status"),
            "evidence_sha256": digest(report), "rejections": validated.get("application_rejections", []),
            "abstention_reason": validated.get("reason")}
