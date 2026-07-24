"""Auto-chorus detection + intro-trim metadata.

WHY
---
Rotor's editor shows operators "this song has a 53s instrumental intro" and
groups repeated chorus lines so they don't look like 4 unrelated rows. Both
are post-processing on the segments we already produce — pure, testable,
no extra Replicate calls.

CONTRACT
--------
- `detect_intro_offset(segs)`: returns the seconds before the first sung
  line. Useful for the banner "Tu canción arranca a 0:53.0" and for the
  render path to actually trim that prefix.
- `mark_repetitions(segs)`: tags segments whose normalized text repeats
  with `repetition_group: N` (1-based, per unique text). Lets the editor
  show "Coro x4" or styled chorus lines. NEVER deletes segments —
  metadata only, the operator decides.
- Both pure. Both safe with empty / malformed inputs.
"""

from __future__ import annotations

import unicodedata
from collections import defaultdict


def _norm(text: str) -> str:
    """Lowercase + strip accents + drop non-alphanumeric — same shape as the
    other modules' normalisation so segments that read the same to a human
    ('Para mí.' / 'Para mí' / 'PARA MI') group as one repetition."""
    s = unicodedata.normalize("NFD", (text or "").lower())
    return "".join(c for c in s if c.isalnum() and not unicodedata.combining(c))


def detect_intro_offset(segs: list[dict]) -> float:
    """Return seconds of instrumental intro before the first sung line.
    Zero (no intro to surface) when the song starts within ~2s. Used by the
    UI banner and by an optional render-side auto-trim."""
    if not segs:
        return 0.0
    try:
        first = float(segs[0].get("start", 0))
    except (TypeError, ValueError):
        return 0.0
    # < 2s isn't worth surfacing — every song has a beat or two of headroom.
    if first < 2.0:
        return 0.0
    return round(first, 2)


def mark_repetitions(segs: list[dict], *, min_count: int = 2) -> list[dict]:
    """Annotate segments whose normalized text repeats `min_count`+ times.
    Returns a NEW list of segs (each a shallow copy) with a `repetition_group`
    integer field set on the repeating ones. Group numbering is 1-based and
    assigned in order of first appearance, so the editor can render
    'Coro #1', 'Coro #2', etc. Never modifies the input segments.

    Example: 'Para mí.' appearing 4 times across the song gets
    repetition_group=1 on all 4 occurrences (regardless of how far apart).
    """
    if not segs or min_count < 2:
        return [dict(s) for s in segs]

    # Count occurrences of each normalized text.
    norm_counts: dict[str, int] = defaultdict(int)
    for s in segs:
        n = _norm(s.get("text", ""))
        if n:
            norm_counts[n] += 1

    # Assign group ids in order of first appearance to texts that repeat.
    norm_to_group: dict[str, int] = {}
    next_id = 1

    out: list[dict] = []
    for s in segs:
        copy = dict(s)
        n = _norm(s.get("text", ""))
        if n and norm_counts.get(n, 0) >= min_count:
            if n not in norm_to_group:
                norm_to_group[n] = next_id
                next_id += 1
            copy["repetition_group"] = norm_to_group[n]
        out.append(copy)
    return out


def annotate(segs: list[dict]) -> tuple[list[dict], dict]:
    """One-shot helper for the pipeline. Returns (segments with `repetition_group`
    annotated, metadata dict). The metadata dict is intended to be merged into
    the API response so the frontend can show banners / chorus styling without
    extra API calls."""
    if not segs:
        return segs, {}
    annotated = mark_repetitions(segs)
    meta = {
        "intro_offset_s": detect_intro_offset(segs),
        "n_repetition_groups": max(
            (s.get("repetition_group", 0) for s in annotated),
            default=0,
        ),
    }
    return annotated, meta
