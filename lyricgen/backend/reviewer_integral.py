"""Offline development primitives. No product writes or correctness certificates.

The spectral arm tests whether a CTC terminal sound continues through blanks.
It tracks spectral shape, not pitch, and deliberately does not certify the
identity of the voice or turn a mixture change-point into a phoneme boundary.
"""
import math
from reviewer_shadow import tokens


def windows(duration, length=24., overlap=6.):
    if not math.isfinite(duration) or duration <= 0 or not 0 < overlap < length <= 24:
        raise ValueError("invalid_coverage_budget")
    result, start = [], 0.
    while start < duration:
        end = min(duration, start + length)
        result.append({"start": start, "end": end, "offset_seconds": start})
        if end == duration:
            break
        start = end - overlap
    return result


def union_seconds(intervals):
    end, total = 0., 0.
    for a, b in sorted(intervals):
        if a < 0 or b < a or not all(math.isfinite(x) for x in (a, b)):
            raise ValueError("invalid_interval")
        total += max(0., b - max(end, a))
        end = max(end, b)
    return round(total, 4)


def locate_words(text, request, window, baseline):
    """Exact lexical occurrences, disambiguated by temporal overlap; not truth."""
    if request.get("tool_status") != "ok":
        return {"status": "recognition_failed", "occurrences": []}
    words = request.get("response", {}).get("words", [])
    flat, owners = [], []
    for i, word in enumerate(words):
        ts = tokens(word.get("word", ""))
        flat.extend(ts)
        owners.extend([i] * len(ts))
    needle = tokens(text)
    found = []
    for i in range(len(flat) - len(needle) + 1) if needle else []:
        if flat[i:i + len(needle)] != needle:
            continue
        a, b = words[owners[i]], words[owners[i + len(needle) - 1]]
        start, end = window["start"] + a["start"], window["start"] + b["end"]
        if not window["start"] <= start < end <= window["end"] + .05:
            continue
        overlap = max(0., min(end, baseline["end"]) - max(start, baseline["start"]))
        found.append({"start": start, "end": end, "overlap": overlap,
                      "local_start": a["start"], "local_end": b["end"],
                      "offset_seconds": window["start"]})
    eligible = [o for o in found if o["overlap"] > 0]
    return {"status": "unique_overlapping_occurrence" if len(eligible) == 1 else
            "occurrence_ambiguous" if len(eligible) > 1 else "phrase_not_recognized_here",
            "occurrences": found, "selected": eligible[0] if len(eligible) == 1 else None,
            "correctness_certified": False}


def spectral_continuity(signal, rate, word_start, word_end, limit):
    """Frozen untuned spectral-shape experiment, native mixture clock.

    Anchor last 120ms of the aligned sound; seek first persistent (80ms)
    spectral change in at most 2s, bounded by next occurrence. No interpolation,
    no fixed addition to the output. These are analysis parameters, not a new
    automatic display-end rule. Chorus/instruments can contaminate this proxy.
    """
    import librosa
    import numpy as np
    result = {"method": "terminal_logmel_shape_v1", "candidate_end": None,
              "target_voice_verified": False, "phonetic_end_supported": False,
              "automatic_apply_allowed": False, "clock": "original_mix_decoded",
              "parameters": {"anchor_seconds": .12, "cosine_min": .90,
                             "persistent_change_seconds": .08, "max_search_seconds": 2.}}
    if not 0 <= word_start < word_end < limit:
        return {**result, "status": "invalid_or_no_postword_context"}
    a = max(word_start, word_end - .12)
    b = min(limit, word_end + 2., len(signal) / rate)
    origin = max(0., a - .08)
    clip = signal[int(origin * rate):int(b * rate)]
    if len(clip) < 1024:
        return {**result, "status": "insufficient_context"}
    mel = librosa.feature.melspectrogram(y=clip, sr=rate, n_fft=1024,
        hop_length=160, n_mels=32, fmin=200, fmax=5000, center=False)
    features = np.log(np.maximum(mel, 1e-10))
    features -= features.mean(axis=0, keepdims=True)  # independent of loudness
    times = origin + (np.arange(features.shape[1]) * 160 + 512) / rate
    anchor = (times >= a) & (times <= word_end)
    if anchor.sum() < 3:
        return {**result, "status": "insufficient_anchor"}
    template = np.median(features[:, anchor], axis=1)
    norm = np.linalg.norm(template)
    similarities = template @ features / np.maximum(norm * np.linalg.norm(features, axis=0), 1e-9)
    if float(np.min(similarities[anchor])) < .90:
        return {**result, "status": "unstable_terminal_anchor"}
    run = []
    for i in np.flatnonzero(times > word_end):
        if similarities[i] < .90:
            run.append(int(i))
            if len(run) >= 8:
                result.update(status="spectral_change_candidate", candidate_end=float(times[run[0]]))
                break
        else:
            run = []
    else:
        result["status"] = "no_observable_boundary_before_limit"
    result.update(anchor_interval=[a, word_end], search_interval=[word_end, b],
        frames=[{"time": round(float(t), 5), "similarity": round(float(s), 6)}
                for t, s in zip(times, similarities)])
    return result
