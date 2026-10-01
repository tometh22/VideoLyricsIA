"""Non-mutating T4 diagnostics for coupled word and line boundaries."""
from __future__ import annotations

import math
from typing import Any, Sequence


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _last_word_end(segment: dict[str, Any]) -> float | None:
    words = segment.get("words")
    if not isinstance(words, list):
        provider = segment.get("provider_evidence")
        words = provider.get("words") if isinstance(provider, dict) else []
    ends = [
        _number(word.get("end")) for word in words or []
        if isinstance(word, dict) and _number(word.get("end")) is not None
    ]
    return max(ends) if ends else None


def analyze_word_line_boundaries(
    segments: Sequence[dict[str, Any]], *, tolerance_s: float = 0.06,
) -> dict[str, Any]:
    """Describe where upstream alignment glued independent clocks together.

    It never chooses a replacement endpoint.  Repeated-section identity and
    acoustic endpoint selection remain separate prerequisites.
    """
    if tolerance_s <= 0:
        raise ValueError("tolerance_s must be positive")
    rows = []
    counts = {
        "segments": len(segments),
        "with_word_clock": 0,
        "segment_end_at_next_start": 0,
        "word_end_at_next_start": 0,
        "coupled_word_line_boundary": 0,
        "fixed_250ms_padding": 0,
    }
    for index, segment in enumerate(segments):
        if not isinstance(segment, dict):
            continue
        segment_end = _number(segment.get("end"))
        word_end = _last_word_end(segment)
        next_start = (
            _number(segments[index + 1].get("start"))
            if index + 1 < len(segments) and isinstance(segments[index + 1], dict)
            else None
        )
        if word_end is not None:
            counts["with_word_clock"] += 1
        segment_at_next = bool(
            segment_end is not None and next_start is not None
            and abs(segment_end - next_start) <= tolerance_s
        )
        word_at_next = bool(
            word_end is not None and next_start is not None
            and abs(word_end - next_start) <= tolerance_s
        )
        padding = (
            segment_end - word_end
            if segment_end is not None and word_end is not None else None
        )
        fixed_padding = bool(
            padding is not None and abs(padding - 0.25) <= 0.075
        )
        coupled = segment_at_next and word_at_next
        counts["segment_end_at_next_start"] += int(segment_at_next)
        counts["word_end_at_next_start"] += int(word_at_next)
        counts["coupled_word_line_boundary"] += int(coupled)
        counts["fixed_250ms_padding"] += int(fixed_padding)
        rows.append({
            "segment_index": index,
            "segment_end": segment_end,
            "last_word_end": word_end,
            "next_line_start": next_start,
            "segment_end_at_next_start": segment_at_next,
            "word_end_at_next_start": word_at_next,
            "coupled_word_line_boundary": coupled,
            "wrapper_padding_s": round(padding, 6) if padding is not None else None,
            "fixed_250ms_padding": fixed_padding,
            "diagnosis": (
                "upstream_shared_word_line_boundary" if coupled
                else "fixed_wrapper_padding" if fixed_padding
                else "independent_or_insufficient_evidence"
            ),
            "automatic_timing_change_allowed": False,
            "cross_occurrence_allowed": False,
        })
    return {
        "schema_version": "word-line-boundary-diagnostics-v1",
        "mutated_segments": False,
        "reference_data_used": False,
        "tolerance_s": tolerance_s,
        "counts": counts,
        "rows": rows,
    }
