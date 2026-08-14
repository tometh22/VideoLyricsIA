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


def _collision_text(segment: dict) -> str:
    return " ".join(str(segment.get("text") or "").split()).casefold()


def _near_collision(left: dict, right: dict) -> bool:
    """Recognize a copied row with a small trailing transcription typo."""
    left_tokens = _collision_text(left).split()
    right_tokens = _collision_text(right).split()
    if not left_tokens or not right_tokens or len(left_tokens) != len(right_tokens):
        return False
    if len(left_tokens) < 3:
        return False
    same_prefix = left_tokens[:-1] == right_tokens[:-1]
    last_a, last_b = left_tokens[-1], right_tokens[-1]
    return same_prefix and (
        last_a.startswith(last_b) or last_b.startswith(last_a)
    )


def _deduplicate_editor_collisions(segments: list[dict]) -> list[dict]:
    """Drop only obvious duplicate rows while retaining the first row.

    Editor edits can append a copy of a lyric to the end of the array.  When
    that copy has the same text and starts within the collision window it is
    not a second sung event; keeping both makes the playback cursor flicker.
    Different text is intentionally preserved because harmonies can overlap.
    """
    out: list[dict] = []
    for segment in segments:
        text = _collision_text(segment)
        try:
            start = float(segment.get("start"))
        except (TypeError, ValueError):
            start = 0.0
        duplicate = next(
            (previous for previous in out
             if (_collision_text(previous) == text or _near_collision(previous, segment))
             and abs(float(previous.get("start") or 0) - start) < 0.35),
            None,
        )
        if duplicate is not None:
            duplicate["end"] = max(
                float(duplicate.get("end") or 0),
                float(segment.get("end") or 0),
            )
            continue
        out.append(dict(segment))
    return out


def canonicalize_editor_segments(segments: Any) -> list[dict]:
    """Canonicalize editor rows without letting a timing regression reorder text.

    A plain timestamp sort fixes rows appended in the middle of a song, but it
    also turns a post-alignment regression into a lyric jump: a later source
    row can be rendered before the line that precedes it semantically.  We
    therefore sort non-overlapping regions by timestamp, while an overlapping
    region that contains a source-order regression keeps its original row
    order.  Only that anomalous region gets a small forward repair.  Legitimate
    monotonic overlaps (harmonies) retain their timestamps.
    """
    if not isinstance(segments, list):
        return []

    cleaned = _deduplicate_editor_collisions([
        segment for segment in segments if isinstance(segment, dict)
    ])
    decorated = []
    for index, segment in enumerate(cleaned):
        try:
            start = float(segment.get("start"))
            end = float(segment.get("end"))
        except (TypeError, ValueError):
            start, end = 0.0, 0.0
        if not math.isfinite(start):
            start = 0.0
        if not math.isfinite(end):
            end = start
        decorated.append({"segment": segment, "index": index,
                          "start": max(0.0, start), "end": max(start, end)})

    by_time = sorted(decorated, key=lambda item: (item["start"], item["index"]))
    regions: list[list[dict]] = []
    for item in by_time:
        if not regions or item["start"] >= max(row["end"] for row in regions[-1]):
            regions.append([item])
        else:
            regions[-1].append(item)

    ordered: list[dict] = []
    for region in regions:
        source_order = sorted(region, key=lambda item: item["index"])
        source_starts = [item["start"] for item in source_order]
        has_regression = any(
            current < previous
            for previous, current in zip(source_starts, source_starts[1:])
        )
        chosen = source_order if has_regression else sorted(
            region, key=lambda item: (item["start"], item["index"]),
        )
        if has_regression:
            ordered.extend(normalize_segments_timing(
                [item["segment"] for item in chosen],
                min_gap=MIN_SEGMENT_GAP,
                min_duration=MIN_SEGMENT_DURATION,
            ))
        else:
            ordered.extend(item["segment"] for item in chosen)
    return ordered


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

    Normal regions retain their individual timings, including legitimate
    overlaps. Only an overlapping region with a source-order regression is
    repaired by ``canonicalize_editor_segments``.
    """
    return canonicalize_editor_segments(segments)


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
