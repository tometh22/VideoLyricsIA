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


# ---------------------------------------------------------------------------
# Final line↔word consistency pass (audit 2026-08-13)
# ---------------------------------------------------------------------------
#
# Several independent stages rewrite a segment's start/end and/or its words —
# anchor_align.build_synced_scaffold, ctc_align, word_vote, phrase_segmenter,
# gap_rescue, chorus_snap, lead_in — and NOTHING verifies, at the end, that a
# line's display window still matches the words it is supposed to show.
#
# Measured across 60 days of production the median is healthy (0.25 s on
# ctc_align, 0.45 s on synced_scaffold) but the tail is broken: p90 is 22.2 s
# on synced_scaffold and the worst ctc_align line is off by 79.7 s. ~10 % of
# all lines and 25 % of synced_scaffold lines end BEFORE their last word is
# sung, i.e. the caption disappears mid-phrase. That is the operator report
# that started this ("líneas que quedaron cortas respecto a lo que se
# escucha", UMG Chile 2026-08-13) — it cost them 48 manual drags on one song.
#
# The scaffold is the worst offender by construction: it sets
# `end = next_line.start - 50 ms`, which is unrelated to when singing stops.
#
# This pass is deliberately asymmetric, because the two failure directions are
# not equally bad:
#   * ending BEFORE the last word is always a defect (text vanishes mid-word);
#   * hanging on AFTER the last word is often intentional (a readability hold,
#     lead_in.polish), so only absurd overhangs are trimmed.
# When the words themselves can't be trusted we leave the timing alone and
# restore the `review` flag instead, so the operator sees the amber marker
# rather than a silently wrong caption.

_CONSISTENCY_UNDERRUN_S = 0.35   # caption dies before the word finishes
_CONSISTENCY_OVERHANG_S = 2.0    # caption lingers absurdly past the last word

# Guards for deciding WHICH words may define a line's span. The existing
# helpers above (`_fa_span_trustworthy`, `_words_fit_window`) were written for
# a different question — "may these forced-align words be attached INSIDE the
# operator's window?" — and are the wrong tool here, verified by test:
#   * `_words_fit_window` demands the words cover >=25% of the CURRENT box, so
#     it rejects exactly the badly-boxed lines this pass exists to fix (the
#     production p90 on synced_scaffold is 22 s of disagreement).
#   * `_fa_span_trustworthy` gates on the MEAN score, so one garbage word does
#     not trip it: the real "Calla, sólo te quiero," line carries a trailing
#     `score: 0.001` token spanning to 38.24 s and still averages 0.64.
# So we filter per-word instead, then require only that the surviving span
# actually overlaps the current window (i.e. it is the same phrase, not one
# from elsewhere in the song).
_MIN_WORD_SCORE = 0.3        # below this a stamp is noise, not a word
_MAX_WORD_DURATION_S = 5.0   # a longer single word is CTC bridging two vocal
                             # events (see ctc_align.repair_bridge_words)


def _consistency_enabled() -> bool:
    return os.environ.get(
        "TIMING_CONSISTENCY_ENABLED", "1",
    ).strip().lower() in _TRUE


def _usable_span(words):
    """(first_start, last_end) over words solid enough to define a line's
    window, or None. Drops low-confidence stamps and implausibly long ones so
    a single bad token can't stretch a caption across the song."""
    starts, ends = [], []
    for w in words:
        if not isinstance(w, dict):
            continue
        try:
            s, e = float(w["start"]), float(w["end"])
        except (TypeError, ValueError, KeyError):
            continue
        if e < s:
            continue
        score = w.get("score")
        if isinstance(score, (int, float)) and float(score) < _MIN_WORD_SCORE:
            continue
        if e - s > _MAX_WORD_DURATION_S:
            continue
        starts.append(s)
        ends.append(e)
    if not starts:
        return None
    return min(starts), max(ends)


def enforce_line_word_consistency(segments):
    """Make every line's display window agree with its own word timings.

    Pure and side-effect free: no audio, no I/O, no network. Returns a NEW
    list when anything changed, otherwise the SAME object it was given (so
    callers can cheaply test `result is not segments`, matching the contract
    of `enrich_segments_with_word_timings` above).

    Asymmetric on purpose — the failure directions are not equally bad:
      * a window ending BEFORE its last word is always a defect (the caption
        vanishes mid-phrase: the operator report that motivated this);
      * a window hanging on AFTER the last word is frequently intentional
        (readability hold, `lead_in.polish`), so only absurd overhangs
        (> _CONSISTENCY_OVERHANG_S) are trimmed;
      * a caption appearing EARLY is deliberate in a lyric video — the viewer
        needs time to read the line before it is sung — so an early start is
        never "corrected". Only a start that lands AFTER the singing already
        began is fixed, because there the viewer misses the opening words.

    When the words cannot be trusted to re-time the line, the timing is left
    untouched and `review` is set instead, so the operator gets the amber
    marker rather than a silently wrong caption. (`ctc_align.finalize_line`
    clears `review` on every line it retimes, which is how a wrong window
    loses its marker in the first place.)

    Never raises: a failure here must not cost a render.
    """
    try:
        if not _consistency_enabled() or not segments:
            return segments

        out = []
        extended = trimmed = flagged = 0
        for seg in segments:
            if not isinstance(seg, dict) or not _has_valid_words(seg):
                out.append(seg)
                continue
            if seg.get("locked") is True or seg.get("operator_locked") is True:
                # The operator's dragged boundary is authoritative.  Timing
                # post-passes may inspect it but must never rewrite it.
                out.append(seg)
                continue
            try:
                ls, le = float(seg.get("start")), float(seg.get("end"))
            except (TypeError, ValueError):
                out.append(seg)
                continue
            if le <= ls:
                out.append(seg)
                continue

            span = _usable_span(seg["words"])
            if span is None:
                out.append(seg)
                continue
            first_word, last_word = span

            underruns_end = le < last_word - _CONSISTENCY_UNDERRUN_S
            # Caption comes up AFTER the words already started → viewer misses
            # the opening of the line. An early start is intentional (see the
            # docstring) and is never touched.
            underruns_start = ls > first_word + _CONSISTENCY_UNDERRUN_S
            overhangs_end = le > last_word + _CONSISTENCY_OVERHANG_S
            if not (underruns_end or underruns_start or overhangs_end):
                out.append(seg)
                continue

            # The window disagrees with the words. Are these even the same
            # phrase? Any overlap at all is enough — demanding a fraction of
            # the CURRENT box would reject the grossly-misboxed lines that are
            # the whole point. Zero overlap means the words aligned somewhere
            # else in the song and must not drag the caption there.
            if min(le, last_word) - max(ls, first_word) <= 0:
                if seg.get("review"):
                    out.append(seg)
                else:
                    new_seg = dict(seg)
                    new_seg["review"] = True
                    out.append(new_seg)
                    flagged += 1
                continue

            new_seg = dict(seg)
            if underruns_end or overhangs_end:
                new_seg["end"] = round(last_word, 3)
                if underruns_end:
                    extended += 1
                else:
                    trimmed += 1
            if underruns_start:
                new_seg["start"] = round(first_word, 3)
            # Degenerate guard: never emit a non-positive window.
            if float(new_seg["end"]) <= float(new_seg["start"]):
                out.append(seg)
                continue
            new_seg["timing_snapped_to_words"] = True
            out.append(new_seg)

        if not (extended or trimmed or flagged):
            return segments
        logger.info(
            "[TIMING-CONSISTENCY] %d/%d lines adjusted "
            "(%d extended, %d trimmed, %d flagged for review)",
            extended + trimmed + flagged, len(segments),
            extended, trimmed, flagged,
        )
        return out
    except Exception as e:  # never break a transcription over this
        logger.warning(
            "[TIMING-CONSISTENCY] pass failed (%s) — leaving segments as-is", e,
        )
        return segments
