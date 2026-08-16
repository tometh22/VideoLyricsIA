"""Bounded acoustic-structure + content mapping for unsafe live windows.

The production v5 route discovers vocal events without text/cardinality and
then maps ASR/Gemini alternatives monotonically.  The older periodic CTC
helpers remain below only for injectable regression compatibility; they are
not authorized to mutate production lyrics.
"""
from __future__ import annotations

import itertools
import logging
import math
import os
import re
import unicodedata
from typing import Callable

import librosa
import numpy as np

logger = logging.getLogger("genly.structural_hybrid")
_TRUE = {"1", "true", "yes", "on"}
_SR = 16_000
_HOP = 320
_VOCALIZATION_TOKENS = {
    "ah", "aha", "eh", "hey", "oh", "ooh", "oooh", "uh", "uoh",
    "uoo", "uou", "woah", "wow", "yeah",
}


def is_enabled() -> bool:
    value = os.environ.get("TARGETED_ACOUSTIC_STRUCTURE_ENABLED")
    if value is None:
        # Backwards-compatible rollout alias.  The old name overstated the
        # implementation: v1 discovers acoustic structure but does not expose
        # a production singing-language CTC verifier.
        value = os.environ.get("TARGETED_ACOUSTIC_CTC_ENABLED", "0")
    return value.strip().lower() in _TRUE


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _token(value: str) -> str:
    value = unicodedata.normalize("NFD", str(value or "").lower())
    value = "".join(char for char in value if unicodedata.category(char) != "Mn")
    return re.sub(r"[^a-z]", "", value)


def _has_vocalization_tail(text: str) -> bool:
    tokens = [_token(part) for part in str(text or "").split()]
    tokens = [part for part in tokens if part]
    return bool(len(tokens) >= 2 and any(
        part in _VOCALIZATION_TOKENS for part in tokens[1:]
    ))


def _ctc_anchor_support(ctc: dict) -> dict:
    """Score the lexical anchor, not an ASR model's ad-lib spelling.

    Spanish XLSR can locate ``Real`` reliably but assigns almost-zero
    probability to equally valid spellings such as ``oh``, ``uoh`` or
    ``wow``.  Averaging those characters made the same acoustic phase pass or
    fail depending only on Gemini's spelling.  For vocalization-rich events we
    therefore use the first non-vocalization word as the phase witness.  Plain
    lexical events and older/injected CTC results retain their line scores.
    """
    anchors = []
    for event in ctc.get("events") or []:
        words = event.get("words") or []
        anchor = next((
            float(word.get("score") or 0)
            for word in words
            if _token(word.get("word") or "")
            and _token(word.get("word") or "") not in _VOCALIZATION_TOKENS
        ), None)
        if anchor is None:
            anchors = []
            break
        anchors.append(anchor)
    if anchors:
        return {
            "mean": float(np.mean(anchors)),
            "min": float(np.min(anchors)),
            "source": "lexical_anchor",
        }
    return {
        "mean": float(ctc.get("mean_score") or ctc.get("median_score") or 0),
        "min": float(ctc.get("min_score") or 0),
        "source": "line",
    }


def _candidate_is_viable(candidate: dict) -> bool:
    return (
        candidate["max_phase_delta"] <= _env_float("TARGETED_CTC_PHASE_MAX", 0.75)
        and candidate["median_phase_delta"] <= _env_float(
            "TARGETED_CTC_PHASE_MEDIAN_MAX", 0.40,
        )
        and candidate["max_anchor_delta"] <= _env_float("TARGETED_CTC_ANCHOR_MAX", 0.85)
        and candidate["stem_support_min"] >= _env_float(
            "TARGETED_CTC_ANCHORED_STEM_MIN", 0.08,
        )
        and candidate["mix_support_min"] >= _env_float(
            "TARGETED_CTC_ANCHORED_MIX_MIN", 0.05,
        )
    )


def _window_vocal_regions(path: str, start: float, end: float) -> list[tuple[float, float]]:
    """Fine-grained energy VAD for a bounded structural window."""
    if not path or not os.path.exists(path):
        return []
    try:
        y, sr = librosa.load(
            path, sr=_SR, mono=True, offset=max(0.0, start),
            duration=max(0.0, end - start),
        )
        if len(y) < _SR:
            return []
        intervals = librosa.effects.split(
            y, top_db=25, frame_length=1024, hop_length=_HOP,
        )
        regions = []
        for left, right in intervals:
            a = start + float(left) / sr
            b = start + float(right) / sr
            if b - a >= 0.12:
                if regions and a - regions[-1][1] <= 0.25:
                    regions[-1] = (regions[-1][0], b)
                else:
                    regions.append((a, b))
        return regions
    except Exception as exc:
        logger.warning("[STRUCTURAL-HYBRID] tail VAD decline %s: %s", path, exc)
        return []


def _extend_vocalization_tails(events: list[dict], texts: list[str],
                               cycle_starts: list[float],
                               regions: list[tuple[float, float]],
                               window_end: float) -> list[dict]:
    """Extend ad-lib lines through acoustically connected vocal regions.

    CTC supplies the lexical onset.  Energy VAD supplies the non-lexical tail,
    whose character scores are not meaningful.  No region may cross into the
    next independently verified cycle.
    """
    if not regions or len(events) != len(texts) == len(cycle_starts):
        return [dict(event) for event in events]
    gaps = np.diff(cycle_starts)
    median_gap = float(np.median(gaps)) if len(gaps) else 4.0
    chain_gap = _env_float("TARGETED_ACOUSTIC_VOICE_CHAIN_GAP_MAX", 1.0)
    out = []
    for index, (event, text) in enumerate(zip(events, texts)):
        updated = dict(event)
        if not _has_vocalization_tail(text):
            out.append(updated)
            continue
        event_start = float(updated.get("start") or cycle_starts[index])
        slot_end = (
            cycle_starts[index + 1] - 0.30
            if index + 1 < len(cycle_starts)
            else min(window_end, cycle_starts[index] + max(2.0, median_gap * 0.88))
        )
        available = [
            (a, b) for a, b in regions
            if b >= event_start - 0.35 and a < slot_end
        ]
        seed = next((position for position, (a, b) in enumerate(available)
                     if a <= event_start + 0.45 and b >= event_start - 0.35), None)
        if seed is None:
            out.append(updated)
            continue
        tail = available[seed][1]
        for a, b in available[seed + 1:]:
            if a - tail > chain_gap:
                break
            tail = max(tail, b)
        acoustic_end = min(slot_end, tail + 0.08)
        if acoustic_end > float(updated.get("end") or event_start) + 0.15:
            updated["end"] = round(acoustic_end, 3)
            # The lexical onset is reliable; per-word ad-lib spans are not.
            # Omitting them prevents false karaoke highlighting inside a line.
            updated.pop("words", None)
            updated["structural_tail_source"] = "vocal_stem_vad"
        out.append(updated)
    return out


def _zscore_rows(values: np.ndarray) -> np.ndarray:
    return (values - np.mean(values, axis=1, keepdims=True)) / (
        np.std(values, axis=1, keepdims=True) + 1e-6
    )


def _features(path: str, start: float, duration: float) -> dict | None:
    try:
        y, _ = librosa.load(
            path, sr=_SR, mono=True, offset=max(0.0, start), duration=duration,
        )
        if len(y) < 2 * _SR:
            return None
        mel = librosa.feature.melspectrogram(
            y=y, sr=_SR, n_fft=1024, hop_length=_HOP,
            n_mels=48, fmin=70, fmax=7600,
        )
        log_mel = librosa.power_to_db(mel, ref=np.max)
        mfcc = librosa.feature.mfcc(S=log_mel, n_mfcc=16)
        delta = librosa.feature.delta(mfcc, width=9)
        chroma = librosa.feature.chroma_stft(
            y=y, sr=_SR, n_fft=1024, hop_length=_HOP,
        )
        rms = librosa.feature.rms(
            y=y, frame_length=1024, hop_length=_HOP,
        )[0]
        onset = librosa.onset.onset_strength(y=y, sr=_SR, hop_length=_HOP)
        pitches, magnitudes = librosa.piptrack(
            y=y, sr=_SR, n_fft=2048, hop_length=_HOP,
            fmin=70, fmax=1000,
        )
        best = np.argmax(magnitudes, axis=0)
        pitch = pitches[best, np.arange(pitches.shape[1])]
        confidence = magnitudes[best, np.arange(magnitudes.shape[1])]
        confidence /= np.max(confidence) + 1e-8
        pitch = np.where(
            confidence >= 0.08,
            np.log2(np.maximum(pitch, 70) / 70),
            0,
        )
        n = min(
            mfcc.shape[1], delta.shape[1], chroma.shape[1],
            len(rms), len(onset), len(pitch),
        )
        dynamics = np.vstack([
            np.log1p(rms[:n]),
            onset[:n] / (np.percentile(onset[:n], 95) + 1e-6),
            pitch[:n], confidence[:n],
        ])
        combined = np.vstack([
            _zscore_rows(mfcc[:, :n]),
            0.65 * _zscore_rows(delta[:, :n]),
            0.45 * _zscore_rows(chroma[:, :n]),
            0.35 * _zscore_rows(dynamics),
        ]).astype(np.float32)
        return {
            "features": combined,
            "rms": rms[:n],
            "onset": onset[:n],
        }
    except Exception as exc:
        logger.warning("[STRUCTURAL-HYBRID] feature decline %s: %s", path, exc)
        return None


def _frame(relative_s: float) -> int:
    return max(0, int(round(relative_s * _SR / _HOP)))


def _dtw_distance(features: np.ndarray, left: float, right: float,
                  duration: float) -> float:
    width = _frame(duration)
    a, b = _frame(left), _frame(right)
    x, y = features[:, a:a + width], features[:, b:b + width]
    if min(x.shape[1], y.shape[1]) < _frame(0.75):
        return 2.0
    cost, _ = librosa.sequence.dtw(
        X=x, Y=y, metric="cosine",
        global_constraints=True, band_rad=0.22,
    )
    return float(cost[-1, -1] / max(x.shape[1], y.shape[1]))


def _boundary_support(bundle: dict, relative_starts: list[float]) -> float:
    rms = np.asarray(bundle["rms"], dtype=float)
    onset = np.asarray(bundle["onset"], dtype=float)
    rms = np.clip(
        (rms - np.percentile(rms, 5)) /
        (np.percentile(rms, 95) - np.percentile(rms, 5) + 1e-8), 0, 1,
    )
    onset = np.clip(
        onset / (np.percentile(onset, 95) + 1e-8), 0, 1,
    )
    supports = []
    radius = _frame(0.18)
    for value in relative_starts:
        index = min(_frame(value), len(rms) - 1)
        lo, hi = max(0, index - radius), min(len(rms), index + radius + 1)
        rise = max(0.0, float(rms[index] - rms[max(0, index - _frame(0.20))]))
        supports.append(max(float(np.max(onset[lo:hi])), rise))
    return float(np.mean(supports)) if supports else 0.0


def _candidate_boundaries(stem: dict, mix: dict, window_start: float) -> list[tuple[float, float]]:
    stem_rms = np.asarray(stem["rms"], dtype=float)
    mix_rms = np.asarray(mix["rms"], dtype=float)
    stem_onset = np.asarray(stem["onset"], dtype=float)
    mix_onset = np.asarray(mix["onset"], dtype=float)

    def normalized(values):
        low, high = np.percentile(values, [5, 95])
        return np.clip((values - low) / (high - low + 1e-8), 0, 1)

    stem_rms, mix_rms = normalized(stem_rms), normalized(mix_rms)
    stem_onset, mix_onset = normalized(stem_onset), normalized(mix_onset)
    peaks = librosa.util.peak_pick(
        stem_onset, pre_max=3, post_max=3, pre_avg=8, post_avg=8,
        delta=0.06, wait=10, sparse=True,
    )
    rises = np.where((stem_rms[1:] >= 0.18) & (stem_rms[:-1] < 0.18))[0] + 1
    raw = sorted(set(int(value) for value in np.concatenate([peaks, rises])))
    candidates = []
    radius = _frame(0.18)
    for index in raw:
        lo, hi = max(0, index - radius), min(len(stem_rms), index + radius + 1)
        stem_support = max(float(stem_onset[index]), float(stem_rms[index]))
        mix_support = max(float(np.max(mix_onset[lo:hi])), float(np.max(mix_rms[lo:hi])))
        support = 0.58 * stem_support + 0.42 * mix_support
        if support >= 0.34:
            candidates.append((window_start + index * _HOP / _SR, support))
    merged = []
    for value in sorted(candidates, key=lambda item: (-item[1], item[0])):
        if all(abs(value[0] - old[0]) >= 0.28 for old in merged):
            merged.append(value)
    return sorted(merged[:80])


def discover_hypotheses(stem_path: str, mix_path: str, count: int,
                        window_start: float, window_end: float) -> list[dict]:
    """Return ranked monotonic cycle hypotheses without using text timing."""
    if not 2 <= count <= 8 or window_end - window_start > 45.0:
        return []
    duration = window_end - window_start
    stem = _features(stem_path, window_start, duration)
    mix = _features(mix_path, window_start, duration)
    if not stem or not mix:
        return []
    boundaries = _candidate_boundaries(stem, mix, window_start)
    times = np.asarray([value[0] for value in boundaries], dtype=float)
    support = {round(value[0], 6): value[1] for value in boundaries}
    if len(times) < count:
        return []
    periods = set()
    for left, right in itertools.combinations(times, 2):
        delta = right - left
        for jumps in range(1, count):
            period = delta / jumps
            if 1.6 <= period <= 10.0:
                periods.add(round(period / 0.12) * 0.12)
    hypotheses = {}
    for first in times:
        for period in periods:
            chosen = [float(first)]
            used = {int(np.argmin(np.abs(times - first)))}
            for index in range(1, count):
                target = first + index * period
                order = np.argsort(np.abs(times - target))
                pick = next((int(value) for value in order if int(value) not in used), None)
                if pick is None or abs(times[pick] - target) > min(0.58, period * 0.16):
                    break
                chosen.append(float(times[pick]))
                used.add(pick)
            if len(chosen) != count or chosen[-1] + 1.0 > window_end:
                continue
            chosen.sort()
            gaps = np.diff(chosen)
            if float(np.min(gaps)) < 1.35:
                continue
            motif_duration = float(np.clip(np.median(gaps) * 0.48, 1.15, 3.2))
            relative = [value - window_start for value in chosen]
            pairs = list(itertools.combinations(relative, 2))
            stem_dtw = float(np.median([
                _dtw_distance(stem["features"], left, right, motif_duration)
                for left, right in pairs
            ]))
            mix_dtw = float(np.median([
                _dtw_distance(mix["features"], left, right, motif_duration)
                for left, right in pairs
            ]))
            regularity = float(np.std(gaps) / (np.mean(gaps) + 1e-6))
            mean_support = float(np.mean([
                support.get(round(value, 6), 0.0) for value in chosen
            ]))
            score = (
                0.53 * stem_dtw + 0.29 * mix_dtw
                + 0.13 * regularity + 0.05 * (1.0 - mean_support)
            )
            key = tuple(round(value / 0.25) for value in chosen)
            candidate = {
                "anchors": [round(value, 3) for value in chosen],
                "period": round(float(np.median(gaps)), 3),
                "stem_dtw": round(stem_dtw, 4),
                "mix_dtw": round(mix_dtw, 4),
                "period_cv": round(regularity, 4),
                "boundary_support": round(mean_support, 4),
                "topology_score": round(score, 5),
            }
            if key not in hypotheses or score < hypotheses[key]["topology_score"]:
                hypotheses[key] = candidate
    ranked = sorted(hypotheses.values(), key=lambda item: item["topology_score"])
    distinct = []
    for candidate in ranked:
        if candidate["stem_dtw"] > 0.68 or candidate["mix_dtw"] > 0.88:
            continue
        if all(
            np.mean(np.abs(np.asarray(candidate["anchors"]) - np.asarray(old["anchors"]))) >= 0.35
            for old in distinct
        ):
            distinct.append(candidate)
        if len(distinct) == 6:
            break
    return distinct


def topology_verdict(stem_path: str, mix_path: str, starts: list[float],
                     window_start: float, window_end: float) -> dict:
    """Confirm recurrence on stem and mix at already text-conditioned starts."""
    if not 2 <= len(starts) <= 8:
        return {"accepted": False, "reason": "invalid_cardinality"}
    if any(not math.isfinite(value) for value in starts):
        return {"accepted": False, "reason": "invalid_start"}
    gaps = np.diff(starts)
    if len(gaps) and float(np.min(gaps)) < 0.55:
        return {"accepted": False, "reason": "events_too_close"}
    duration = window_end - window_start
    stem = _features(stem_path, window_start, duration)
    mix = _features(mix_path, window_start, duration)
    if not stem or not mix:
        return {"accepted": False, "reason": "feature_extraction_failed"}
    relative = [value - window_start for value in starts]
    motif_duration = float(np.clip(np.median(gaps) * 0.48, 1.15, 3.2))
    pairs = list(itertools.combinations(relative, 2))
    stem_dtw = float(np.median([
        _dtw_distance(stem["features"], left, right, motif_duration)
        for left, right in pairs
    ]))
    mix_dtw = float(np.median([
        _dtw_distance(mix["features"], left, right, motif_duration)
        for left, right in pairs
    ]))
    stem_boundary = _boundary_support(stem, relative)
    mix_boundary = _boundary_support(mix, relative)
    regularity = float(np.std(gaps) / (np.mean(gaps) + 1e-6))
    accepted = (
        stem_dtw <= _env_float("TARGETED_ACOUSTIC_STEM_DTW_MAX", 0.58)
        and mix_dtw <= _env_float("TARGETED_ACOUSTIC_MIX_DTW_MAX", 0.78)
        and stem_boundary >= _env_float("TARGETED_ACOUSTIC_STEM_BOUNDARY_MIN", 0.28)
        and mix_boundary >= _env_float("TARGETED_ACOUSTIC_MIX_BOUNDARY_MIN", 0.22)
        and regularity <= _env_float("TARGETED_ACOUSTIC_PERIOD_CV_MAX", 0.28)
    )
    return {
        "accepted": accepted,
        "reason": "verified" if accepted else "topology_threshold",
        "stem_dtw": round(stem_dtw, 4),
        "mix_dtw": round(mix_dtw, 4),
        "stem_boundary": round(stem_boundary, 4),
        "mix_boundary": round(mix_boundary, 4),
        "period_cv": round(regularity, 4),
        "motif_duration": round(motif_duration, 3),
    }


def verify(stem_path: str, mix_path: str, events: list[dict], *,
           window_start: float, window_end: float, job_id: str = "",
           ctc_fn: Callable | None = None,
           topology_fn: Callable | None = None,
           anchor_ctc_fn: Callable | None = None,
           hypotheses_fn: Callable | None = None,
           content_hypotheses: list[dict] | None = None) -> dict:
    """Return a replacement candidate only after all witnesses agree."""
    stats = {"accepted": False, "reason": "disabled", "events": []}
    if not is_enabled():
        return stats
    injected_legacy_path = any(
        value is not None
        for value in (ctc_fn, topology_fn, anchor_ctc_fn, hypotheses_fn)
    )
    if not injected_legacy_path:
        # Production v5: discover acoustic events before looking at any text.
        # The previous implementation passed len(events) into a nearly
        # periodic grid search, so a mistaken Gemini cardinality became an
        # acoustic "fact".  Keep that implementation below only as an
        # injectable compatibility surface for focused legacy tests.
        try:
            from acoustic_structure import analyze_window
            from phonetic_verifier import verify_content

            structure = analyze_window(
                stem_path, mix_path, window_start=window_start,
                window_end=window_end,
            )
            stats["acoustic_structure"] = structure
            if not structure.get("accepted"):
                stats["reason"] = f"structure_{structure.get('reason') or 'declined'}"
                return stats
            hypotheses = list(content_hypotheses or [])
            hypotheses.insert(0, {
                "source": "gemini_audio", "family": "gemini_audio",
                "events": [dict(event) for event in (events or [])],
            })
            mapping, phonetic = verify_content(
                stem_path, mix_path, structure, hypotheses,
                window_start=window_start, window_end=window_end,
            )
            stats["content_mapping"] = mapping
            stats["phonetic_evidence"] = phonetic
            stats["phase_margin"] = mapping.get("margin")
            if not mapping.get("events"):
                stats["reason"] = f"mapping_{mapping.get('reason') or 'declined'}"
                return stats
            proposed = []
            for event in mapping["events"]:
                proposed.append({
                    **dict(event),
                    "review": True,
                    "consensus_reprocessed": True,
                    "structural_repair": True,
                    "structural_hybrid": True,
                    "timing_source": "acoustic_dp_ctc_v1",
                    "acoustic_event_ids": [event.get("id")],
                    "structure_confidence": event.get("confidence"),
                    "mapping_confidence": round(
                        max(0.0, min(1.0, float(mapping.get("margin") or 0))), 4,
                    ),
                    "consensus_sources": [
                        "acoustic_structure_v1", "vocal_stem_correlated_mix",
                        str(event.get("content_source") or "content_lattice"),
                    ],
                })
            stats.update({
                "accepted": bool(mapping.get("accepted")),
                "reason": "candidate_for_review" if mapping.get("accepted")
                else str(mapping.get("reason") or "ambiguous_mapping"),
                "events": proposed,
                "suggested_events": proposed,
                # Certification permits an operator-facing suggestion only.
                # Automatic mutation remains locked behind the signed release
                # gate, separately from model confidence.
                "automatic_apply_allowed": False,
                "calibration": "pending_benchmark",
            })
            return stats
        except Exception as exc:
            logger.warning("[STRUCTURAL-HYBRID-V5] decline: %r job=%s", exc, job_id)
            stats["reason"] = f"exception:{type(exc).__name__}"
            return stats
    texts = [str(event.get("text") or "").strip() for event in (events or [])]
    if not 2 <= len(texts) <= 8 or any(not text for text in texts):
        stats["reason"] = "invalid_candidate"
        return stats
    try:
        # Production path: waveform topology proposes several phases, then a
        # text-conditioned CTC pass scores each phase in independently bounded
        # slots on stem and mix.  This prevents global Viterbi from spending a
        # repeated line on unrelated voice at the start of the unsafe window.
        if ctc_fn is None:
            if hypotheses_fn is None:
                hypotheses_fn = discover_hypotheses
            if anchor_ctc_fn is None:
                from ctc_align import align_structural_anchors
                anchor_ctc_fn = align_structural_anchors
            hypotheses = hypotheses_fn(
                stem_path, mix_path, len(texts), window_start, window_end,
            ) or []
            stats["topology_hypotheses"] = hypotheses
            if not hypotheses:
                stats["reason"] = "topology_hypotheses_unavailable"
                return stats
            scored = []
            for hypothesis in hypotheses:
                anchors = [float(value) for value in hypothesis.get("anchors") or []]
                if len(anchors) != len(texts):
                    continue
                stem_ctc = anchor_ctc_fn(
                    stem_path, texts, anchors,
                    window_start, window_end, job_id,
                )
                mix_ctc = anchor_ctc_fn(
                    mix_path, texts, anchors,
                    window_start, window_end, job_id,
                )
                if not stem_ctc or not mix_ctc:
                    continue
                stem_events = stem_ctc.get("events") or []
                mix_events = mix_ctc.get("events") or []
                if len(stem_events) != len(texts) or len(mix_events) != len(texts):
                    continue
                stem_starts = np.asarray([float(event["start"]) for event in stem_events])
                mix_starts = np.asarray([float(event["start"]) for event in mix_events])
                anchor_values = np.asarray(anchors)
                max_phase = float(np.max(np.abs(stem_starts - mix_starts)))
                median_phase = float(np.median(np.abs(stem_starts - mix_starts)))
                max_anchor = float(np.max(np.abs(stem_starts - anchor_values)))
                stem_support = _ctc_anchor_support(stem_ctc)
                mix_support = _ctc_anchor_support(mix_ctc)
                evidence_score = (
                    0.50 * stem_support["mean"]
                    + 0.35 * mix_support["mean"]
                    - 0.15 * float(hypothesis.get("topology_score") or 1)
                )
                candidate = {
                    "evidence_score": round(evidence_score, 5),
                    "hypothesis": hypothesis,
                    "stem_ctc": stem_ctc,
                    "mix_ctc": mix_ctc,
                    "max_phase_delta": round(max_phase, 4),
                    "median_phase_delta": round(median_phase, 4),
                    "max_anchor_delta": round(max_anchor, 4),
                    "stem_support_mean": round(stem_support["mean"], 5),
                    "stem_support_min": round(stem_support["min"], 5),
                    "stem_support_source": stem_support["source"],
                    "mix_support_mean": round(mix_support["mean"], 5),
                    "mix_support_min": round(mix_support["min"], 5),
                    "mix_support_source": mix_support["source"],
                }
                candidate["viable"] = _candidate_is_viable(candidate)
                scored.append(candidate)
            # Prefer a viable representative when two topology proposals
            # collapse to the same CTC-derived phase.  Otherwise a high-score
            # but cross-witness-invalid duplicate could suppress the valid
            # representative before the uncertainty check.
            scored.sort(
                key=lambda item: (item["viable"], item["evidence_score"]),
                reverse=True,
            )
            # Multiple onset detectors often produce sub-frame variants of the
            # same phase.  They are not independent alternatives and must not
            # collapse the uncertainty margin.  Deduplicate by the CTC-derived
            # starts, not by the original topology anchors.
            distinct_scored = []
            for candidate in scored:
                candidate_starts = np.asarray([
                    float(event["start"])
                    for event in candidate["stem_ctc"].get("events") or []
                ])
                if all(
                    len(old["stem_ctc"].get("events") or []) != len(candidate_starts)
                    or np.mean(np.abs(
                        candidate_starts - np.asarray([
                            float(event["start"])
                            for event in old["stem_ctc"]["events"]
                        ])
                    )) >= 0.35
                    for old in distinct_scored
                ):
                    distinct_scored.append(candidate)
            stats["scored_hypotheses"] = distinct_scored
            if not distinct_scored:
                stats["reason"] = "anchored_ctc_unavailable"
                return stats
            # A phase that already disagrees across stem/mix, misses its
            # acoustic anchors, or lacks lexical support is not an alternative
            # interpretation.  Letting it compete in the uncertainty margin
            # caused valid phases to be rejected by demonstrably invalid ones.
            viable = [candidate for candidate in distinct_scored if candidate["viable"]]
            stats["viable_hypotheses"] = len(viable)
            if not viable:
                stats["reason"] = "anchored_ctc_ambiguous"
                return stats
            best = viable[0]
            second = viable[1] if len(viable) > 1 else None
            margin = (
                best["evidence_score"] - second["evidence_score"]
                if second else 1.0
            )
            stats["phase_margin"] = round(float(margin), 5)
            stem_ctc, mix_ctc = best["stem_ctc"], best["mix_ctc"]
            if margin < _env_float("TARGETED_CTC_PHASE_MARGIN_MIN", 0.025):
                stats["reason"] = "anchored_ctc_ambiguous"
                return stats
            anchors = best["hypothesis"]["anchors"]
            topology_checker = topology_fn or topology_verdict
            topology = topology_checker(
                stem_path, mix_path, anchors, window_start, window_end,
            )
            stats["topology"] = topology
            if not topology.get("accepted"):
                stats["reason"] = "topology_disagreement"
                return stats
            aligned_events = _extend_vocalization_tails(
                stem_ctc["events"], texts,
                [float(event["start"]) for event in stem_ctc["events"]],
                _window_vocal_regions(stem_path, window_start, window_end),
                window_end,
            )
            verified_events = []
            for source, aligned in zip(events, aligned_events):
                verified_events.append({
                    **dict(aligned),
                    "text": str(source.get("text") or aligned.get("text") or "").strip(),
                    "review": False,
                    "consensus_reprocessed": True,
                    "structural_repair": True,
                    "structural_hybrid": True,
                    "consensus_sources": [
                        "gemini_audio_cardinality",
                        "ctc_vocal_stem",
                        "ctc_original_mix",
                        "acoustic_topology_stem_mix",
                    ],
                })
            stats.update({
                "accepted": True, "reason": "verified",
                "events": verified_events,
                "stem_ctc": stem_ctc, "mix_ctc": mix_ctc,
                "max_phase_delta": best["max_phase_delta"],
                "median_phase_delta": best["median_phase_delta"],
            })
            return stats

        # Injectable legacy-shaped path used by focused unit tests and as a
        # compact verifier surface for callers that already own CTC evidence.
        if topology_fn is None:
            topology_fn = topology_verdict
        stem_ctc = ctc_fn(
            stem_path, texts, window_start, window_end, job_id,
        )
        mix_ctc = ctc_fn(
            mix_path, texts, window_start, window_end, job_id,
        )
        stats["stem_ctc"] = stem_ctc
        stats["mix_ctc"] = mix_ctc
        if not stem_ctc or not mix_ctc:
            stats["reason"] = "ctc_witness_unavailable"
            return stats
        stem_events = stem_ctc.get("events") or []
        mix_events = mix_ctc.get("events") or []
        if len(stem_events) != len(texts) or len(mix_events) != len(texts):
            stats["reason"] = "ctc_cardinality_disagreement"
            return stats
        stem_starts = [float(event["start"]) for event in stem_events]
        mix_starts = [float(event["start"]) for event in mix_events]
        deltas = np.abs(np.asarray(stem_starts) - np.asarray(mix_starts))
        max_delta = float(np.max(deltas))
        median_delta = float(np.median(deltas))
        stats["max_phase_delta"] = round(max_delta, 4)
        stats["median_phase_delta"] = round(median_delta, 4)
        if (
            max_delta > _env_float("TARGETED_CTC_PHASE_MAX", 0.75)
            or median_delta > _env_float("TARGETED_CTC_PHASE_MEDIAN_MAX", 0.40)
            or float(stem_ctc.get("min_score") or 0) < _env_float("TARGETED_CTC_STEM_SCORE_MIN", 0.24)
            or float(mix_ctc.get("min_score") or 0) < _env_float("TARGETED_CTC_MIX_SCORE_MIN", 0.20)
        ):
            stats["reason"] = "ctc_phase_or_score_disagreement"
            return stats

        # Stem timing wins after the independent mix has selected the same
        # phase.  Averaging would blur the stronger vocal boundary.
        topology = topology_fn(
            stem_path, mix_path, stem_starts, window_start, window_end,
        )
        stats["topology"] = topology
        if not topology or not topology.get("accepted"):
            stats["reason"] = "topology_disagreement"
            return stats
        verified_events = []
        for source, aligned in zip(events, stem_events):
            verified_events.append({
                **dict(aligned),
                "text": str(source.get("text") or aligned.get("text") or "").strip(),
                "review": False,
                "consensus_reprocessed": True,
                "structural_repair": True,
                "structural_hybrid": True,
                "consensus_sources": [
                    "gemini_audio_cardinality",
                    "ctc_vocal_stem",
                    "ctc_original_mix",
                    "acoustic_topology_stem_mix",
                ],
            })
        stats.update({
            "accepted": True,
            "reason": "verified",
            "events": verified_events,
        })
        return stats
    except Exception as exc:
        logger.warning("[STRUCTURAL-HYBRID] decline: %r job=%s", exc, job_id)
        stats["reason"] = f"exception:{type(exc).__name__}"
        return stats
