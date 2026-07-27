"""Post-reconcile segment cleanup.

Pure, import-light helpers that run after whisperx_reconcile.reconcile() to
reduce manual corrections in the editor. Split into its own module so it can
be unit-tested without pulling in the full pipeline.py import chain.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("genly.post_reconcile")

# ── Tunables ──────────────────────────────────────────────────────────────────

ADLIB_MIN_DUR: float = 6.0      # only chunk ad-lib blocks longer than this (s)
ADLIB_CHUNK_DUR: float = 3.5    # target seconds per output chunk
ADLIB_MIN_REPEATS: int = 5      # _is_single_word_loop min_repeats for SPLIT path
                                 # (lower than hallucination filter's 8 — we want
                                 # to SPLIT the block rather than drop it)
LONG_LINE_MIN_WORDS: int = 7    # canonical word count that triggers gap-split
LONG_LINE_MIN_DUR: float = 3.0  # segment must also be this long (s)
LONG_LINE_GAP_THRESH: float = 0.7  # internal word-to-word silence (s) → split
# Deprecated compatibility constant. Positive gaps no longer trigger any
# extension/trim; word timestamps determine phrase ends.
STRETCH_GAP_THRESH: float = 1.5
STRETCH_MARGIN: float = 0.08      # leave this gap before next segment after trim


# ── Helpers ───────────────────────────────────────────────────────────────────

def _normalize_token(tok: str) -> str:
    import re
    return re.sub(r"[^a-z0-9áéíóúüñ]", "", tok.lower())


def _is_adlib_loop(text: str) -> bool:
    """True if text is essentially the same short ad-lib token repeated.

    Uses the same logic as pipeline._is_single_word_loop() but with a lower
    min_repeats so we catch blocks the hallucination filter would NOT drop
    (blocks of 5-7 repeats that are musically real but visually need splitting).
    """
    if not text:
        return False
    tokens = [n for n in (_normalize_token(w) for w in text.split()) if n]
    if len(tokens) < ADLIB_MIN_REPEATS:
        return False
    from collections import Counter
    counts = Counter(tokens)
    top_token, top_count = counts.most_common(1)[0]
    concentration = top_count / len(tokens)
    # Allow tokens longer than 4 chars only when they're 100 % of tokens
    # (e.g. "Uoh-oh-oh-oh-oh" repeated × 5 normalises to "uohohohohoh" but
    # is unambiguously a melismatic ad-lib block). Mixed cases (e.g. "uh" +
    # "uoh-oh-oh-oh-oh") hit the ≤4-char branch on the short token instead.
    if len(top_token) > 4 and concentration < 1.0:
        return False
    return concentration >= 0.85 and top_count >= ADLIB_MIN_REPEATS


def _clean_adlib_chunk_text(words: list[dict]) -> str:
    """Join word tokens into a display label for an ad-lib chunk."""
    parts = []
    for w in words:
        tok = (w.get("word") or "").strip().strip(",.")
        if tok:
            parts.append(tok)
    return ", ".join(parts) if parts else ""


def _split_adlib_by_time(seg: dict, words: list[dict]) -> list[dict]:
    """Split a long ad-lib segment into chunks of ~ADLIB_CHUNK_DUR seconds.

    When word-level stamps are present (always after reconcile) we close a
    chunk at the first word whose midpoint crosses the chunk boundary so the
    split lands at a real spoken boundary. Text is reconstructed from the
    whisperX words in each chunk (the lrclib mega-line text is meaningless
    when sliced).
    """
    dur = seg["end"] - seg["start"]

    if not words:
        # No word stamps — equal-time split. Distribute text tokens across
        # chunks so each chunk gets a SHORT label (e.g. "uh, uh, uh") instead
        # of repeating the full mega-text. Repeating the full text in every
        # chunk re-triggers _has_fuzzy_intra_loop (needs < 12 tokens per
        # segment to stay below the detection floor).
        n = max(2, round(dur / ADLIB_CHUNK_DUR))
        step = dur / n
        text_tokens = [t for t in (seg.get("text") or "").split() if t]
        toks_per = max(1, round(len(text_tokens) / n)) if text_tokens else 1
        chunks = []
        for i in range(n):
            t_start = i * toks_per
            t_end = min((i + 1) * toks_per, len(text_tokens))
            chunk_text = " ".join(text_tokens[t_start:t_end]) if t_start < len(text_tokens) else (text_tokens[-1] if text_tokens else "")
            chunks.append({
                "start": round(seg["start"] + i * step, 3),
                "end": round(seg["start"] + (i + 1) * step, 3) if i < n - 1 else seg["end"],
                "text": chunk_text,
            })
        return chunks

    out_segs: list[dict] = []
    chunk_words: list[dict] = []
    chunk_start = seg["start"]
    next_boundary = chunk_start + ADLIB_CHUNK_DUR

    for i, w in enumerate(words):
        chunk_words.append(w)
        w_mid = (w.get("start", 0) + w.get("end", w.get("start", 0))) / 2
        is_last = i == len(words) - 1

        if not is_last and w_mid >= next_boundary:
            next_w_start = words[i + 1].get("start", w.get("end", seg["end"]))
            chunk_end = min(next_w_start, seg["end"])
            chunk_text = _clean_adlib_chunk_text(chunk_words)
            out_segs.append({
                "start": chunk_start,
                "end": chunk_end,
                "text": chunk_text,
                "words": list(chunk_words),
            })
            chunk_start = next_w_start
            next_boundary = chunk_start + ADLIB_CHUNK_DUR
            chunk_words = []

    if chunk_words:
        chunk_text = _clean_adlib_chunk_text(chunk_words)
        out_segs.append({
            "start": chunk_start,
            "end": seg["end"],
            "text": chunk_text,
            "words": list(chunk_words),
        })

    return out_segs if len(out_segs) > 1 else [seg]


def _split_at_word_gaps(seg: dict, words: list[dict]) -> list[dict]:
    """Split a segment at ALL internal word-to-word silences >= LONG_LINE_GAP_THRESH.

    Uses the canonical text (seg["text"]) for each sub-line label. The wx word
    stream maps 1:1 to canonical words after reconcile (wordstamps_to_segments
    grabs exactly len(canonical_words) tokens per line), so the split index in
    one stream equals the split index in the other.

    Splits at every gap above threshold (not just the largest), so a
    three-phrase line like
        "Como tantos vas | buscando las respuestas | que nada te responden."
    yields three segments instead of two.
    """
    if len(words) < 4:
        return [seg]

    # Reconciled segments historically re-attached words by ordinal position.
    # When a catalogue line was absent from the audio, those words could belong
    # to a *different* line and even sit outside this segment's bounds. Splitting
    # that invalid mapping produced reversed intervals (start > end), then the
    # global sort interleaved fragments from adjacent lines. Never transform a
    # segment unless its acoustic evidence is internally monotonic and contained.
    try:
        seg_start_bound = float(seg["start"])
        seg_end_bound = float(seg["end"])
        previous_start = seg_start_bound
        for word in words:
            word_start = float(word["start"])
            word_end = float(word["end"])
            if (
                word_end < word_start
                or word_start < previous_start
                or word_start < seg_start_bound - 0.25
                or word_end > seg_end_bound + 0.25
            ):
                return [seg]
            previous_start = word_start
    except (KeyError, TypeError, ValueError):
        return [seg]

    canonical_words = (seg.get("text") or "").split()
    n_can = len(canonical_words)
    n_wx = len(words)

    # Collect all inter-word gaps that exceed the threshold.
    split_indices: list[int] = []  # split AFTER word at this index
    for i in range(len(words) - 1):
        w_end = words[i].get("end", words[i].get("start", 0.0))
        w_next = words[i + 1].get("start", w_end)
        if w_next - w_end >= LONG_LINE_GAP_THRESH:
            split_indices.append(i)

    if not split_indices:
        return [seg]

    # Build output segments chunk by chunk.
    out_segs: list[dict] = []
    chunk_wx_start = 0
    seg_start = seg["start"]

    for split_after in split_indices:
        chunk_wx = words[chunk_wx_start: split_after + 1]
        next_wx_word = words[split_after + 1] if split_after + 1 < len(words) else None

        # Map wx range → canonical range (1:1 when n_wx == n_can; approx otherwise).
        can_lo = round(n_can * chunk_wx_start / n_wx) if n_wx > 0 else chunk_wx_start
        can_hi = round(n_can * (split_after + 1) / n_wx) if n_wx > 0 else split_after + 1
        can_lo = max(0, min(can_lo, n_can))
        can_hi = max(can_lo + 1, min(can_hi, n_can))
        chunk_text = " ".join(canonical_words[can_lo:can_hi])

        if chunk_wx and chunk_text:
            chunk_end = chunk_wx[-1].get("end", chunk_wx[-1].get("start", seg_start))
            out_segs.append({
                "start": seg_start,
                "end": chunk_end,
                "text": chunk_text,
                "words": list(chunk_wx),
            })

        seg_start = next_wx_word.get("start", seg_start) if next_wx_word else seg["end"]
        chunk_wx_start = split_after + 1

    # Final chunk.
    last_wx = words[chunk_wx_start:]
    can_lo = round(n_can * chunk_wx_start / n_wx) if n_wx > 0 else chunk_wx_start
    last_text = " ".join(canonical_words[max(0, can_lo):])
    if last_wx and last_text:
        out_segs.append({
            "start": seg_start,
            "end": seg["end"],
            "text": last_text,
            "words": list(last_wx),
        })

    if len(out_segs) <= 1:
        return [seg]
    try:
        if any(
            float(part["end"]) <= float(part["start"])
            for part in out_segs
        ):
            return [seg]
        if any(
            float(out_segs[i]["start"]) < float(out_segs[i - 1]["end"])
            for i in range(1, len(out_segs))
        ):
            return [seg]
    except (KeyError, TypeError, ValueError):
        return [seg]
    return out_segs


# ── Main entry point ──────────────────────────────────────────────────────────

def post_reconcile_cleanup(
    segments: list[dict],
    *,
    split_long_lines: bool = True,
) -> list[dict]:
    """Post-process reconciled segments to reduce manual correction work.

    Two passes (each segment processed once — no recursion):

    1. **Ad-lib chunker**: any segment where _is_adlib_loop() is True AND
       duration >= ADLIB_MIN_DUR gets split into ~ADLIB_CHUNK_DUR-second chunks.
       Addresses the "26-second Uh, uh, uh... mega-block" pattern where lrclib
       stores a whole ad-lib section as one line.

    2. **Long-line gap splitter** (when ``split_long_lines`` is true): any
       segment with >= LONG_LINE_MIN_WORDS
       canonical words AND duration >= LONG_LINE_MIN_DUR gets split at ALL
       internal word gaps >= LONG_LINE_GAP_THRESH. Addresses the "Iluminados
       por el fuego / que dejaste arder" merging pattern where lrclib collapses
       two (or more) sung phrases into one line.

    Gated by POST_RECONCILE_CLEANUP_ENABLED env var (default "1").
    """
    if os.environ.get("POST_RECONCILE_CLEANUP_ENABLED", "1").strip().lower() not in (
        "1", "true", "yes", "on"
    ):
        return segments

    out: list[dict] = []
    for seg in segments:
        words = seg.get("words") or []
        text = seg.get("text") or ""
        dur = float(seg.get("end", 0)) - float(seg.get("start", 0))

        # Pass 1: ad-lib chunker
        if dur >= ADLIB_MIN_DUR and _is_adlib_loop(text):
            chunks = _split_adlib_by_time(seg, words)
            if len(chunks) > 1:
                logger.info(
                    "[POST_RECONCILE] ad-lib split: %.1fs → %d chunks (%r…)",
                    dur, len(chunks), text[:40],
                )
            out.extend(chunks)
            continue

        # Pass 2: long-line gap splitter
        if (
            split_long_lines
            and len(words) >= LONG_LINE_MIN_WORDS
            and dur >= LONG_LINE_MIN_DUR
        ):
            parts = _split_at_word_gaps(seg, words)
            if len(parts) > 1:
                logger.info(
                    "[POST_RECONCILE] long-line split: %d words → %d parts (%r…)",
                    len(words), len(parts), text[:40],
                )
            out.extend(parts)
            continue

        out.append(seg)

    # Pass 3: tighten display ends to the last acoustically timestamped word.
    #
    # The previous implementation detected a positive silence gap and then set
    # ``end = next_start - margin``. That does the opposite of trimming: a line
    # ending at 84 s followed by the next vocal at 127 s was extended across the
    # instrumental break. "La Foto de tu cuerpo" exposed outputs lasting up to
    # 37 s on the raw path because of this.
    #
    # Word timestamps are the only positive evidence of where the phrase ends.
    # Tighten to the last word when Whisper's segment boundary trails it. With
    # no words, preserve the original end; never invent a hold through silence.
    # Independently clamp true overlaps so one line cannot run into the next.
    out2: list[dict] = []
    for i, seg in enumerate(out):
        seg = dict(seg)
        try:
            seg_start = float(seg.get("start", 0))
            seg_end = float(seg.get("end", seg_start))
            words = seg.get("words") or []
            valid_word_ends = []
            for word in words:
                try:
                    word_end = float(word.get("end"))
                except (TypeError, ValueError, AttributeError):
                    continue
                if seg_start <= word_end <= seg_end:
                    valid_word_ends.append(word_end)
            if valid_word_ends:
                last_word_end = max(valid_word_ends)
                if last_word_end < seg_end:
                    logger.info(
                        "[POST_RECONCILE] word-end tighten: %.3f→%.3f %r…",
                        seg_end, last_word_end, (seg.get("text") or "")[:30],
                    )
                    seg["end"] = last_word_end
                    seg_end = last_word_end
        except (TypeError, ValueError):
            pass
        if i + 1 < len(out):
            try:
                next_start = float(out[i + 1].get("start", seg.get("end", 0)))
                current_end = float(seg.get("end", 0))
                if current_end >= next_start:
                    trimmed_end = round(next_start - STRETCH_MARGIN, 3)
                    if trimmed_end > float(seg.get("start", 0)):
                        logger.info(
                            "[POST_RECONCILE] overlap trim: %.3f→%.3f %r…",
                            current_end, trimmed_end,
                            (seg.get("text") or "")[:30],
                        )
                        seg["end"] = trimmed_end
            except (TypeError, ValueError):
                pass
        out2.append(seg)
    return out2
