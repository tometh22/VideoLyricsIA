"""Shared guards for line-level lyric timing.

The transcription and alignment stages preserve the semantic order of lyric
lines.  A later timing pass may move one line backwards, though, which leaves
the payload internally non-monotonic and makes playback selection jump.  This
module repairs that narrow failure mode without sorting the text into a
different lyric order.
"""

from __future__ import annotations

import math
from typing import Any


MIN_SEGMENT_GAP = 0.05
MIN_SEGMENT_DURATION = 0.3


def sort_segments_chronologically(segments: Any) -> list[dict]:
    """Return valid segment dictionaries in stable chronological order.

    This is intentionally separate from ``normalize_segments_timing``. The
    transcription pipeline must preserve semantic lyric order while repairing
    a regressed timestamp; editor persistence, on the other hand, needs a
    timeline order because users can insert a line in the middle of a song
    while React temporarily appends it to the array.
    """
    if not isinstance(segments, list):
        return []

    decorated = []
    for index, segment in enumerate(segments):
        if not isinstance(segment, dict):
            continue
        try:
            start = float(segment.get("start"))
        except (TypeError, ValueError):
            start = 0.0
        if not math.isfinite(start):
            start = 0.0
        decorated.append((max(0.0, start), index, segment))

    return [segment for _, _, segment in sorted(decorated, key=lambda item: (item[0], item[1]))]


def timing_anomalies(segments: Any) -> dict[str, int]:
    """Summarize order/overlap anomalies without changing the payload."""
    if not isinstance(segments, list):
        return {"regressions": 0, "overlaps": 0, "duplicate_starts": 0}

    starts = []
    ends = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        try:
            start = float(segment.get("start"))
            end = float(segment.get("end"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(start) or not math.isfinite(end):
            continue
        starts.append(start)
        ends.append(end)

    regressions = sum(current < previous for previous, current in zip(starts, starts[1:]))
    ordered_intervals = sorted(zip(starts, ends), key=lambda item: (item[0], item[1]))
    overlaps = 0
    furthest_end = None
    for current_start, current_end in ordered_intervals:
        if furthest_end is not None and furthest_end > current_start:
            overlaps += 1
        furthest_end = max(furthest_end or current_end, current_end)
    rounded_starts = [round(start, 3) for start in starts]
    duplicate_starts = len(rounded_starts) - len(set(rounded_starts))
    return {
        "regressions": regressions,
        "overlaps": overlaps,
        "duplicate_starts": duplicate_starts,
    }


def normalize_editor_segments(segments: Any) -> list[dict]:
    """Canonicalize user-edited segments for persistence/rendering.

    Unlike transcription repair, editor canonicalization must not invent a
    50 ms gap between simultaneous rows or move a timestamp to preserve the
    incoming array order. The timestamp is the editor's ordering authority;
    stable sorting keeps equal-time rows deterministic while preserving their
    individual timings and metadata.
    """
    return normalize_segments_timing(
        sort_segments_chronologically(segments),
        min_gap=0,
    )


def normalize_segments_timing(
    segments: Any,
    *,
    min_gap: float = MIN_SEGMENT_GAP,
    min_duration: float = MIN_SEGMENT_DURATION,
) -> list[dict]:
    """Keep segment starts monotonic while preserving semantic row order.

    A stable sort by timestamp would repair the numeric order but can put a
    lyric line before the preceding lyric phrase when an aligner nudges one
    start backwards.  The editor's row order is the source of truth for lyric
    text, so we move only regressed starts forward and preserve each line's
    original duration.  Invalid timings get the same conservative fallback.
    """
    if not isinstance(segments, list):
        return []

    safe_gap = max(0.0, float(min_gap))
    safe_duration = max(0.001, float(min_duration))
    normalized: list[dict] = []
    previous_start: float | None = None

    for segment in segments:
        if not isinstance(segment, dict):
            continue

        raw_start = segment.get("start")
        raw_end = segment.get("end")
        try:
            start = float(raw_start)
        except (TypeError, ValueError):
            start = 0.0 if previous_start is None else previous_start + safe_gap
        if not math.isfinite(start):
            start = 0.0 if previous_start is None else previous_start + safe_gap
        start = max(0.0, start)

        try:
            end = float(raw_end)
        except (TypeError, ValueError):
            end = start + safe_duration
        if not math.isfinite(end):
            end = start + safe_duration

        original_duration = max(safe_duration, end - start)
        if previous_start is not None:
            start = max(start, previous_start + safe_gap)
        end = max(start + safe_duration, start + original_duration)

        normalized.append({
            **segment,
            "start": round(start, 4),
            "end": round(end, 4),
        })
        previous_start = start

    return normalized
