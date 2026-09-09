"""Diagnostic only: never invent, trim or remove timestamps/text.

A long line or pause alone is not a failure. Flag a large *internal word gap*
only with corroborating degenerate word support in that line or the next one.
Repeated words remain separate occurrences. Native probabilities are not used
as calibrated recognition/CTC scores. Missing words alone are not an alert.
"""
import math

SCHEMA = "word-timing-validation-v1"
MIN_INTERNAL_GAP_S = 8.0


def _words(segment):
    current = segment.get("words")
    return current if isinstance(current, list) and current else (
        (segment.get("provider_evidence") or {}).get("words") or [])


def _bounds(word):
    try:
        start, end = float(word["start"]), float(word["end"])
        if math.isfinite(start) and math.isfinite(end) and start >= 0 and end > start:
            return start, end
    except (KeyError, TypeError, ValueError):
        pass
    return None


def diagnose(segments):
    rows = []
    for segment in segments:
        words = _words(segment)
        bounds = [_bounds(w) for w in words]
        rows.append((words, bounds, sum(b is None for b in bounds)))
    findings = []
    for index, (words, bounds, unusable) in enumerate(rows):
        reasons, gaps = [], []
        # Consecutive provider entries only: never bridge absent/zero-duration
        # words and pretend the endpoints support the whole intervening text.
        for word_index, (left, right) in enumerate(zip(bounds, bounds[1:])):
            if left and right and right[0] - left[1] >= MIN_INTERNAL_GAP_S:
                gaps.append({"left_word_index": word_index,
                             "right_word_index": word_index + 1,
                             "start": left[1], "end": right[0],
                             "gap_seconds": round(right[0] - left[1], 4)})
        next_unusable = rows[index + 1][2] if index + 1 < len(rows) else 0
        if gaps and (unusable >= 2 or next_unusable >= 2):
            reasons.append("internal_word_gap_with_degenerate_support")
        if len(words) >= 2 and unusable >= 2 and unusable * 2 >= len(words):
            reasons.append("word_timing_support_unusable")
        if reasons:
            findings.append({"schema": SCHEMA, "status": "unvalidated",
                             "segment_index": index, "reasons": reasons,
                             "internal_gaps": gaps, "unusable_words": unusable,
                             "word_count": len(words), "next_unusable_words": next_unusable,
                             "text_error_confirmed": False, "repair_applied": False})
    return findings
