"""Karaoke word-timing enrichment — on-demand, cached.

The karaoke fill (libass ``\\kf``) needs per-word timestamps. Production
``segments_json`` is line-level only (the operator editor and the LRClib
alignment path don't carry ``words``), so ``ass_render._word_timings`` falls
back to SYNTHESIS — splitting each line proportional to character count. That
drifts on held / uneven notes (client report: "some lines sync, others lag").

This module derives REAL per-word timing by force-aligning the approved lyrics
against the audio ONCE, and merges the word stamps onto the approved segments.

Design contract (deliberately conservative — see the plan):
  * Gated by the CALLER to ``lyrics_animation == "karaoke"`` — never runs for
    other jobs.
  * Touches NOTHING in the transcription pipeline.
  * Never raises; on disable / failure / no-match it returns the input
    segments UNCHANGED, so the render falls back to today's synthesis (no
    regression). The worst case equals current behaviour.
  * The operator's per-line ``start``/``end`` (the *when* a line shows) is
    preserved; only ``words`` (the per-word fill) is attached. ``_word_timings``
    clamps words into the line window and enforces monotonicity at render time.
"""
from __future__ import annotations

import logging
import unicodedata

logger = logging.getLogger(__name__)


def _norm(text: str) -> str:
    """Lowercase + alphanumerics only — same shape as
    ass_render._strip_for_match, so the words we attach survive that path's
    token-match guard when the line text is unchanged."""
    out = unicodedata.normalize("NFC", text or "").lower()
    return "".join(ch for ch in out if ch.isalnum())


def _has_valid_words(seg: dict) -> bool:
    w = seg.get("words")
    return (
        isinstance(w, list)
        and len(w) > 0
        and all(isinstance(x, dict) and "start" in x and "end" in x for x in w)
    )


def enrich_segments_with_word_timings(segments, audio_path):
    """Attach per-word timing to `segments` for karaoke fill, via forced align.

    Returns the segments UNCHANGED (no-op) when they already carry word timing,
    when forced-align is disabled/unavailable, or when anything fails. Never
    raises. The caller must gate this to karaoke jobs.
    """
    try:
        if not segments or not audio_path:
            return segments

        line_segs = [s for s in segments if str(s.get("text", "")).strip()]
        if not line_segs:
            return segments
        # Already aligned (e.g. a previous render cached words) → nothing to do.
        if all(_has_valid_words(s) for s in line_segs):
            return segments

        import forced_align
        if not forced_align.is_enabled():
            return segments

        lyrics_text = "\n".join(str(s.get("text", "")).strip() for s in line_segs)
        aligned = forced_align.forced_align_lyrics(audio_path, lyrics_text)
        if not aligned:
            return segments

        # Map aligned lines back onto the approved segments by normalized text,
        # order-preserving with a small forward window so repeated lines and
        # dropped/re-anchored lines don't desync the mapping.
        merged = []
        cursor = 0
        attached = 0
        for seg in segments:
            txt = str(seg.get("text", "")).strip()
            if not txt or _has_valid_words(seg):
                merged.append(seg)
                continue
            key = _norm(txt)
            found = None
            j = cursor
            while j < len(aligned) and j < cursor + 6:
                cand = aligned[j]
                if cand.get("words") and _norm(str(cand.get("text", ""))) == key:
                    found = cand
                    cursor = j + 1
                    break
                j += 1
            if found:
                new_seg = dict(seg)
                new_seg["words"] = found["words"]  # keep operator start/end
                merged.append(new_seg)
                attached += 1
            else:
                merged.append(seg)

        if not attached:
            logger.info("[KARAOKE] forced-align returned no matching lines — using synthesis")
            return segments
        logger.info("[KARAOKE] attached forced-align word timing to %d/%d lines",
                    attached, len(line_segs))
        return merged
    except Exception as e:  # never break a render over karaoke timing
        logger.warning("[KARAOKE] word-timing enrichment failed (%s) — using synthesis", e)
        return segments
