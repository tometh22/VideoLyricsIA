#!/usr/bin/env python3
"""Align LRCLib plain lines to Whisper word_timestamps.

When LRCLib has the song's lyrics text (line structure curated by humans)
but no synced timing, the current pipeline falls back to Whisper's own
segmentation. Whisper groups lines differently than LRCLib — it merges
short adjacent lines and splits long ones — which produces karaoke
output that doesn't match human line structure.

This module provides `align_lrclib_to_whisper(plain, segments_w_words)`
that keeps LRCLib's line boundaries and pulls timing from Whisper's
word-level timestamps. Output is the same `[{start, end, text}]` shape
the renderer consumes.

Algorithm:
  1. Flatten Whisper segments into a single ordered word stream
  2. For each LRCLib line, search forward in the stream for the
     contiguous word span whose concatenated text best matches the line
     (SequenceMatcher ratio). Try spans of length close to the LRCLib
     line's word count.
  3. Use first word's start and last word's end as the segment timing.
  4. Advance the cursor past the matched span — preserves song order
     and avoids matching the same Whisper words to multiple lines.

The lookahead is bounded so an unmatchable line (Whisper missed it
entirely) doesn't poison the rest of the song.
"""

import difflib
import re
from typing import Optional


_NORMALIZE_RE = re.compile(r"[^\w\sáéíóúñü]", re.IGNORECASE)


def _normalize(text: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace."""
    t = (text or "").lower()
    t = _NORMALIZE_RE.sub(" ", t)
    return " ".join(t.split())


def _flatten_words(whisper_segments: list[dict]) -> list[dict]:
    """Pull all per-word timings out of Whisper's segment structure into
    a single ordered stream. Each entry: {word, start, end}."""
    out = []
    for seg in whisper_segments:
        for w in seg.get("words") or []:
            word_text = (w.get("word") or "").strip()
            if not word_text:
                continue
            try:
                start = float(w.get("start"))
                end = float(w.get("end"))
            except (TypeError, ValueError):
                continue
            out.append({"word": word_text, "start": start, "end": end})
    return out


def _best_span_for_line(
    line_norm: str,
    line_word_count: int,
    stream: list[dict],
    cursor: int,
    max_lookahead: int,
    min_ratio: float,
) -> Optional[tuple[int, int, float]]:
    """Find the contiguous span in `stream[cursor:cursor+max_lookahead]`
    whose concatenated text best matches `line_norm`.

    Returns (start_idx, end_idx_exclusive, ratio) or None if no span
    above `min_ratio` was found.
    """
    end_search = min(cursor + max_lookahead, len(stream))
    # Try span lengths slightly under to over the expected line length.
    # Lower bound 1 so single-word lines ("Oh", "Uh") still match.
    # Upper bound +5 allows Whisper to have a couple extra words (filler
    # like "ah ah" between content words).
    min_len = max(1, line_word_count - 2)
    max_len = line_word_count + 5

    best = None
    best_ratio = 0.0
    for span_start in range(cursor, end_search):
        # Cap span length at remaining stream
        remaining = len(stream) - span_start
        for span_len in range(min_len, min(max_len, remaining) + 1):
            span_text = " ".join(
                stream[i]["word"] for i in range(span_start, span_start + span_len)
            )
            span_norm = _normalize(span_text)
            r = difflib.SequenceMatcher(None, line_norm, span_norm).ratio()
            if r > best_ratio:
                best_ratio = r
                best = (span_start, span_start + span_len, r)
        # Small optimization: if we already have a near-perfect match,
        # don't keep searching wildly far ahead.
        if best_ratio > 0.92:
            break

    if best and best[2] >= min_ratio:
        return best
    return None


_HALLUCINATION_MARKERS = (
    "suscríbete", "suscribete", "subscribe",
    "gracias por ver", "thanks for watching",
    "subtítulos", "subtitles",
    "música", "music",  # standalone, single-word seg
    "¡gracias!", "¡suscríbete!",
)


def _is_hallucination(text: str) -> bool:
    """Whisper sometimes invents YouTube outro phrases (\"Suscríbete al canal\",
    \"Música\", etc.) when it can't recognise the actual audio. These never
    match real LRCLib lines and just shift the cursor, so we drop them
    before alignment.
    """
    if not text:
        return True
    t = _normalize(text)
    if not t:
        return True
    # Single-word filler ("Música") or known marketing phrases
    if t in {"musica", "gracias"}:
        return True
    for marker in _HALLUCINATION_MARKERS:
        if marker in t:
            return True
    return False


def align_lrclib_to_whisper(
    lrclib_plain: str,
    whisper_segments: list[dict],
    *,
    min_ratio: float = 0.72,
    max_lookahead: int = 25,
) -> list[dict]:
    """Build segments using LRCLib's line structure + Whisper's timing.

    Args:
        lrclib_plain: multi-line lyrics string from LRCLib (no timing).
            Each non-empty line becomes (potentially) one output segment.
        whisper_segments: Whisper result, expected shape
            [{"start", "end", "text", "words": [{"word", "start", "end"}, ...]}, ...].
            If `words` is missing on a segment, that segment is skipped
            for word-level matching.
        min_ratio: minimum SequenceMatcher ratio (0..1) to accept a span
            match. Below this, the LRCLib line is dropped from the
            output (Whisper didn't transcribe it well enough).
        max_lookahead: how many Whisper words ahead of the cursor to
            search per LRCLib line. Bounded to keep alignment O(L · W).

    Returns:
        [{"start": float, "end": float, "text": str}, ...] one per
        LRCLib line that found a confident match. Empty list if either
        input is empty or no matches above threshold.

    Behaviour notes:
      - Lines that don't match well (whisper missed them, or transcribed
        garbage) are simply skipped — they won't appear in the output.
        This is conservative: better to drop a line than mistime it.
      - The cursor only advances forward, so the output is monotonically
        ordered by start time, matching the original song order.
    """
    if not lrclib_plain or not whisper_segments:
        return []

    # Filter out Whisper hallucinations (outro phrases, "Música", etc.)
    # at the SEGMENT level — if a whole segment is a hallucination, drop
    # all its words from the stream so they can't be matched.
    filtered_segments = [
        seg for seg in whisper_segments
        if not _is_hallucination(seg.get("text") or "")
    ]
    stream = _flatten_words(filtered_segments)
    if not stream:
        return []

    out = []
    cursor = 0
    for raw_line in lrclib_plain.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        line_norm = _normalize(line)
        if not line_norm:
            continue
        line_word_count = len(line_norm.split())

        match = _best_span_for_line(
            line_norm, line_word_count, stream, cursor,
            max_lookahead=max_lookahead, min_ratio=min_ratio,
        )
        if not match:
            # Whisper didn't transcribe this line well enough to align.
            # Skip rather than guess — the operator can add it manually
            # in the editor if needed.
            continue
        s, e, _ratio = match
        out.append({
            "start": float(stream[s]["start"]),
            "end": float(stream[e - 1]["end"]),
            "text": line,
        })
        cursor = e

    return out
