"""WhisperX × reference-lyrics reconciliation.

WHY
---
WhisperX gives us word-level timing pinned to the actual audio (truth for
timestamps), but its TEXT — what it heard the singer say — can be rough
on names, mondegreens, or odd phrasings. Meanwhile lrclib/Gemini gives us
curated TEXT with proper line breaks and spelling (truth for the lyric)
but no timestamps tied to *this* audio.

The combination is what beats Rotor: whisperX's audio-anchored word stamps
+ lrclib's clean text = best of both. Implementation reuses the hardened
`wordstamps_to_segments` (forced_align.py) which is already designed to
bucket a word-stream into known lyric lines with drift detection.

CONTRACT
--------
- `reconcile(wx_segs, reference_text) -> list[dict] | None`: returns
  segments with REFERENCE text + WHISPERX timing, or None when
  reconciliation looks unreliable (caller falls back to wx_segs).
- Pure (no I/O); the only dependency is `forced_align.wordstamps_to_segments`.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("genly.whisperx_reconcile")


def _flatten_words(wx_segs: list[dict]) -> list[dict]:
    """Concatenate the per-word arrays into one ordered stream. Skips segs
    without word-stamps (whisperX provides them only when align_output=True;
    forced_align doesn't, so reconciliation is whisperX-only)."""
    words: list[dict] = []
    for s in wx_segs or []:
        for w in s.get("words") or []:
            if isinstance(w, dict):
                words.append(w)
    return words


def reconcile(wx_segs: list[dict],
              reference_text: str,
              *, min_coverage: float = 0.5) -> list[dict] | None:
    """Re-bucket whisperX word-stamps into the reference text's line
    structure. Returns segments with reference text + whisperX timing, or
    None if reconciliation can't produce enough lines.

    `min_coverage` — fraction of reference lines that must be assigned
    timing for the result to be trusted. Below the threshold, the caller
    keeps whisperX's own segmentation."""
    words = _flatten_words(wx_segs)
    if len(words) < 8:
        logger.info("[RECONCILE] not enough word stamps (%s) — skip", len(words))
        return None
    lines = [ln.strip() for ln in (reference_text or "").splitlines() if ln.strip()]
    if len(lines) < 4:
        logger.info("[RECONCILE] reference too short (%s lines) — skip", len(lines))
        return None

    # `wordstamps_to_segments` (forced_align.py) is the hardened helper that
    # walks a word stream against a known line structure with fuzzy
    # re-anchoring + drift abort. Returns [] when it gives up.
    from forced_align import wordstamps_to_segments
    out = wordstamps_to_segments(words, lines)
    if not out:
        logger.warning("[RECONCILE] wordstamps_to_segments aborted (drift) — keep whisperX")
        return None
    coverage = len(out) / max(1, len(lines))
    if coverage < min_coverage:
        logger.warning(
            "[RECONCILE] thin coverage %s/%s (%.0f%%) — keep whisperX",
            len(out), len(lines), coverage * 100,
        )
        return None

    # Re-attach per-word stamps to each reconciled line so the editor can
    # still do word-level karaoke. We just bucket the same `words` again by
    # position so the words inside line N align with line N's text.
    line_word_counts = [len(ln.split()) for ln in lines if ln]
    cur = 0
    for i, seg in enumerate(out):
        # `out` may have fewer entries than lines (drop on monotonic clamp);
        # we can't reliably re-attach in that case, so leave words off.
        try:
            wc = line_word_counts[i]
        except IndexError:
            break
        span = words[cur:cur + wc]
        cur += wc
        if span and all(isinstance(w, dict) and "start" in w for w in span):
            seg["words"] = [
                {"word": w.get("word", "").strip(),
                 "start": float(w.get("start", seg["start"])),
                 "end": float(w.get("end", seg["end"]))}
                for w in span
            ]

    logger.info(
        "[RECONCILE] %s/%s lines reconciled (%.0f%% coverage) — adopting reference text + whisperX timing",
        len(out), len(lines), coverage * 100,
    )
    return out
