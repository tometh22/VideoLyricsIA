"""Structural lyric diagnostics that require a listen before any correction."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import math
import re
import unicodedata
from typing import Any


SCHEMA_VERSION = "lyric-observation-v1"
_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
_OUTRO_PHRASES = frozenset({
    "gracias por ver el video",
    "gracias por ver este video",
    "subtitulos por la comunidad de amara org",
    "subtitles by the amara org community",
    "thank you for watching",
    "thanks for watching",
})


def _tokens(value: Any) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKD", str(value or "").casefold())
    unaccented = "".join(letter for letter in normalized if not unicodedata.combining(letter))
    return tuple(_TOKEN.findall(unaccented))


def _bounds(row: Mapping[str, Any]) -> tuple[float, float] | None:
    try:
        start = float(row.get("start", row.get("s")))
        end = float(row.get("end", row.get("e")))
    except (ValueError, TypeError, OverflowError):
        return None
    if math.isfinite(start) and math.isfinite(end) and 0 <= start < end:
        return start, end
    return None


def _word_clocks(
    row: Mapping[str, Any], display_tokens: set[str],
) -> set[tuple[tuple[str, ...], float, float]]:
    words = row.get("words")
    if not isinstance(words, list) or not words:
        evidence = row.get("provider_evidence")
        words = evidence.get("words") if isinstance(evidence, Mapping) else None
    if not isinstance(words, list):
        return set()
    clocks = set()
    for word in words:
        if not isinstance(word, Mapping):
            continue
        tokens = _tokens(word.get("word") or word.get("text") or word.get("t"))
        bounds = _bounds(word)
        # Human splits can retain a copy of the full original ASR evidence.
        # Only clocks for words displayed in both cards can be shared here.
        if tokens and bounds and display_tokens.issuperset(tokens):
            clocks.add((tokens, *bounds))
    return clocks


def build_lyric_observation(segments: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Return timestamped candidates tied to the caller's exact segment snapshot."""
    findings = []
    event_indices = {}
    previous_clocks = set()
    for index, segment in enumerate(segments):
        if not isinstance(segment, Mapping):
            previous_clocks = set()
            continue
        bounds = _bounds(segment)
        tokens = _tokens(segment.get("text") or segment.get("t"))
        clocks = _word_clocks(segment, set(tokens)) if bounds else set()
        if bounds and tokens:
            start, end = bounds
            event = (tokens, start, end)
            previous_index = event_indices.setdefault(event, index)
            if previous_index != index:
                findings.append({
                    "code": "duplicate_lyric_event", "segment_indices": [previous_index, index],
                    "start": start, "end": end,
                })
            else:
                shared = previous_clocks & clocks
                if shared:
                    findings.append({
                        "code": "reused_word_clock", "segment_indices": [index - 1, index],
                        "start": min(clock[1] for clock in shared),
                        "end": max(clock[2] for clock in shared),
                        "shared_word_count": len(shared),
                    })
            if " ".join(tokens) in _OUTRO_PHRASES:
                findings.append({
                    "code": "possible_outro_caption", "segment_indices": [index],
                    "start": start, "end": end,
                })
        previous_clocks = clocks if tokens else set()
    return {"schema_version": SCHEMA_VERSION, "mode": "observe", "findings": findings}
