"""Native-clock sustain interval experiment; observation, not a timing rule."""
import math


def bracket_sustain(times, f0, voiced, *, anchor_start, anchor_end, ceiling):
    """Follow contiguous stable pitch from the aligned final word, not a pause.

    Missing pitch is an abstention, not silence. A measured loss of voicing
    brackets periodicity only; it does not certify lead-vocal phonetics or the
    acceptable visual endpoint. No interpolation over gaps or added padding.
    """
    anchors = [i for i, t in enumerate(times) if anchor_start <= t <= anchor_end
               and voiced[i] and math.isfinite(float(f0[i])) and f0[i] > 0]
    if not anchors:
        return {"status": "no_voiced_anchor", "interval": None}
    index = anchors[-1]
    reference = float(f0[index])
    last = index
    for i in range(index + 1, len(times)):
        if times[i] >= ceiling:
            break
        if not voiced[i] or not math.isfinite(float(f0[i])):
            return {"status": "periodicity_boundary_observed", "interval": [times[last], times[i]],
                    "pitch_reference_hz": reference, "target_voice_certified": False,
                    "phonetic_end_certified": False, "perceptual_gold_interval": False}
        if abs(12 * math.log2(float(f0[i]) / reference)) > 2:
            return {"status": "pitch_changed_not_word_end", "interval": None,
                    "last_stable_time": times[last]}
        last = i
    return {"status": "context_boundary_without_observed_end", "interval": None,
            "last_stable_time": times[last]}


def measure(audio_path, alignment, *, next_occurrence_start=None):
    import numpy as np
    import librosa
    import time
    from reviewer_shadow_audio import pcm

    begin = time.monotonic()
    result = {"tool": "pyin_native_mix_interval_v1", "clock": "original_mix_decoded",
              "automatic_apply_allowed": False, "cross_signal_timestamp_transfer": False}
    try:
        words = alignment.get("words", [])
        if not words:
            return {**result, "status": "word_not_localized", "interval": None}
        start, end = alignment["window"]["start"], alignment["window"]["end"]
        if not 0 < end - start <= 24:
            raise ValueError("window_budget_exceeded")
        sr = 16000
        signal = pcm(audio_path, rate=sr)[round(start * sr):round(end * sr)]
        f0, voiced, probability = librosa.pyin(signal, fmin=70, fmax=800, sr=sr,
            frame_length=1024, hop_length=160)
        times = (np.arange(len(f0)) * .01 + start).tolist()
        last = words[-1]
        result.update(bracket_sustain(times, f0, voiced,
            anchor_start=last["global_start"], anchor_end=last["global_end"],
            ceiling=min(end, next_occurrence_start if next_occurrence_start is not None else end)))
        result.update(anchor_word=last, configuration={"fmin": 70, "fmax": 800,
            "frame_length": 1024, "hop_length": 160, "pitch_tolerance_semitones": 2},
            voiced_frame_fraction=float(np.mean(voiced)),
            frame_evidence=[{"time": t, "f0": float(f) if math.isfinite(float(f)) else None,
                "voiced": bool(v), "probability": float(p)}
                for t, f, v, p in zip(times, f0, voiced, probability)])
    except Exception as exc:
        result.update(status="tool_error", error_type=type(exc).__name__, error_message=str(exc)[:300])
    result["latency_seconds"] = time.monotonic() - begin
    return result
