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
import os
import unicodedata

logger = logging.getLogger(__name__)

_TRUE = {"1", "true", "yes", "on"}


def _norm(text: str) -> str:
    """Lowercase + alphanumerics only — same shape as
    ass_render._strip_for_match, so the words we attach survive that path's
    token-match guard when the line text is unchanged."""
    out = unicodedata.normalize("NFC", text or "").lower()
    return "".join(ch for ch in out if ch.isalnum())


def _retime_enabled() -> bool:
    """Opt-in (default OFF): when forced-align yields a HIGH-CONFIDENCE span for
    a line, TRUST it and RE-TIME the line (override the operator/whisperX
    window) instead of only filling words inside the existing window.

    Why: on sustained/repetitive vocals whisperX drifts the LINE timing itself.
    The window guard (`_words_fit_window`) then rejects the *correct* forced
    align timing because it disagrees with the *wrong* window → the line drops
    to synthesis and the karaoke sweep desyncs (client report: Mercedes Sosa
    "Hablando A Tu Corazón"). Re-timing from a confident FA span fixes BOTH the
    line sync and the per-word fill. Default off so behaviour is unchanged
    until validated in staging."""
    return os.environ.get("KARAOKE_FA_RETIME_ENABLED", "0").strip().lower() in _TRUE


def _fa_confidence(words) -> float:
    """Mean per-word score (0-1) when the model returned probabilities; 1.0 when
    no scores are present so coherence alone can gate older responses."""
    scores = [
        float(w["score"]) for w in words
        if isinstance(w, dict) and isinstance(w.get("score"), (int, float))
    ]
    return sum(scores) / len(scores) if scores else 1.0


def _fa_span_trustworthy(words, *, min_conf: float = 0.55) -> bool:
    """Is a forced-align word span solid enough to RE-TIME a line onto it?
    Requires real, ordered start/end stamps, a non-degenerate span, and mean
    confidence >= ``min_conf``. Rejects empty/degenerate spans so a bad align
    can never re-time a line onto garbage (it falls through to today's guard)."""
    if not isinstance(words, list) or not words:
        return False
    starts, ends = [], []
    for w in words:
        if not (isinstance(w, dict) and "start" in w and "end" in w):
            return False
        try:
            s, e = float(w["start"]), float(w["end"])
        except (TypeError, ValueError):
            return False
        if e < s:
            return False
        starts.append(s)
        ends.append(e)
    if max(ends) - min(starts) <= 0:
        return False
    if starts[0] > ends[-1]:  # monotone sanity
        return False
    return _fa_confidence(words) >= min_conf


def _words_fit_window(words, line_start, line_end) -> bool:
    """Gross-misalignment guard: only trust forced-align words for a line if
    their time span actually overlaps the operator's line window. Tolerant of
    small operator timing shifts (those still overlap heavily); rejects words
    that aligned to a DIFFERENT part of the song (which would otherwise clamp
    into a degenerate sweep and look worse than synthesis). Needs the FA span
    to cover at least ~25% of the line. Returns True when timings are missing
    (let the render-time clamp handle it)."""
    try:
        ls, le = float(line_start), float(line_end)
    except (TypeError, ValueError):
        return True
    dur = le - ls
    if dur <= 0:
        return True
    starts = [float(w["start"]) for w in words if isinstance(w, dict) and "start" in w]
    ends = [float(w["end"]) for w in words if isinstance(w, dict) and "end" in w]
    if not starts or not ends:
        return True
    fs, fe = min(starts), max(ends)
    overlap = max(0.0, min(le, fe) - max(ls, fs))
    return overlap >= 0.25 * dur


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
        retimed = 0
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
            if (
                found
                and _retime_enabled()
                and not seg.get("locked")  # never override a manual re-sync
                and _fa_span_trustworthy(found["words"])
            ):
                # Confident FA span → TRUST it: re-time the line from the FA
                # word span (override a drifted whisperX window) AND attach the
                # words. This is the case the guard below would wrongly reject
                # because the operator/whisperX window is itself off. A line the
                # operator locked (manually synced) is never re-timed.
                fw = found["words"]
                new_seg = dict(seg)
                new_seg["start"] = min(float(w["start"]) for w in fw)
                new_seg["end"] = max(float(w["end"]) for w in fw)
                new_seg["words"] = fw
                new_seg["retimed_by_forced_align"] = True
                merged.append(new_seg)
                attached += 1
                retimed += 1
            elif found and _words_fit_window(found["words"], seg.get("start"), seg.get("end")):
                new_seg = dict(seg)
                new_seg["words"] = found["words"]  # keep operator start/end
                merged.append(new_seg)
                attached += 1
            else:
                # No match, or FA words grossly off this line's window → leave
                # the line on synthesis (today's behaviour) rather than risk a
                # worse, misaligned sweep.
                merged.append(seg)

        if not attached:
            logger.info("[KARAOKE] forced-align returned no matching lines — using synthesis")
            return segments
        logger.info(
            "[KARAOKE] attached forced-align word timing to %d/%d lines (%d re-timed)",
            attached, len(line_segs), retimed,
        )
        return merged
    except Exception as e:  # never break a render over karaoke timing
        logger.warning("[KARAOKE] word-timing enrichment failed (%s) — using synthesis", e)
        return segments
