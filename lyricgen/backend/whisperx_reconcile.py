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


def text_correct_segments(audio_segs: list[dict],
                          reference_text: str,
                          *, min_match: float = 0.60) -> list[dict]:
    """Audio-as-truth text correction (line-level).

    Keeps each audio segment's timing, order, structure and word-stamps; only
    swaps its TEXT to the best-matching reference line. Segments whose best
    reference match is below `min_match` (e.g. sustained ad-libs like
    "uh uh uh" that lyric sites omit) are returned UNCHANGED.

    Unlike `reconcile()` / `wordstamps_to_segments`, this does NOT flatten the
    word stream or re-bucket it into the reference's line structure — so a
    reference that LACKS the song's ad-lib section can never scatter the
    surrounding chorus words across the ad-lib gap (the smearing bug). It's a
    per-line spell-checker over whisperX's OWN segments, not a re-segmenter.

    Matching: per-segment best reference line via max(token-Jaccard,
    phonetic-ratio) — phonetic rescues mondegreens ("perro de voz" vs "espejo
    de vos") that Jaccard misses. Assignment is a globally-optimal MONOTONIC DP
    so a chorus repeated N times maps to its N reference occurrences in order
    (not all to the first). Returns a NEW list; never mutates the input.
    """
    if not audio_segs:
        return audio_segs
    out_default = [dict(s) for s in audio_segs]
    ref_lines = [ln.strip() for ln in (reference_text or "").splitlines() if ln.strip()]
    if len(ref_lines) < 2:
        return out_default  # nothing to correct against

    from forced_align import _norm, _phonetic_ratio
    from lyrics_cleanup_alignment import _jaccard, _tokens

    seg_txt = [(s.get("text") or "") for s in audio_segs]
    seg_jac = [_tokens(t) for t in seg_txt]
    seg_pho = [[_norm(w) for w in t.split() if _norm(w)] for t in seg_txt]
    ref_jac = [_tokens(l) for l in ref_lines]
    ref_pho = [[_norm(w) for w in l.split() if _norm(w)] for l in ref_lines]

    def score(i: int, j: int) -> float:
        return max(_jaccard(seg_jac[i], ref_jac[j]),
                   _phonetic_ratio(seg_pho[i], ref_pho[j]))

    n, m = len(audio_segs), len(ref_lines)
    EPS = 1e-6  # earliest-ref tie-break, same idea as _match_cleaned_to_synced
    # dp[i][j] = best total match score over segs[i:] using refs[j:].
    # actions: match seg i↔ref j | skip_ref j (never sung) | skip_seg i (ad-lib).
    dp = [[0.0] * (m + 1) for _ in range(n + 1)]
    choice: list[list[str | None]] = [[None] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            best, act = dp[i + 1][j], "skip_seg"          # seg i unmatched
            if dp[i][j + 1] > best:
                best, act = dp[i][j + 1], "skip_ref"      # ref j unused
            sc = score(i, j)
            if sc >= min_match:
                mv = sc + EPS * (m - j) + dp[i + 1][j + 1]
                if mv > best:
                    best, act = mv, "match"
            dp[i][j], choice[i][j] = best, act

    assign: list[int | None] = [None] * n
    i = j = 0
    while i < n and j < m:
        act = choice[i][j]
        if act == "match":
            assign[i] = j; i += 1; j += 1
        elif act == "skip_ref":
            j += 1
        else:
            i += 1

    # Build output:
    #   - matched seg → swap text to its reference line.
    #   - unmatched seg with real text (ad-lib "uh") → keep as-heard.
    #   - unmatched BLANK seg (a melisma whisper heard but couldn't transcribe,
    #     e.g. a sustained "Frágil espejo de vos") → name it from the reference
    #     line skipped right here (timing stays the audio segment's, flagged
    #     `review`); drop it if no reference line fits. This is SAFE: it only
    #     names segments the audio already produced — it never fabricates a line
    #     where there's no audio (which would spam phantom chorus repeats).
    matched_refs = {a for a in assign if a is not None}
    out: list[dict] = []
    n_swapped = n_filled = 0
    last_ref = -1
    for idx, s in enumerate(audio_segs):
        j = assign[idx]
        if j is not None:
            seg = dict(s)
            if (seg.get("text") or "").strip() != ref_lines[j]:
                n_swapped += 1
            seg["text"] = ref_lines[j]
            out.append(seg)
            last_ref = j
            continue
        if (s.get("text") or "").strip():
            out.append(dict(s))   # ad-lib / unmatched-but-real text → keep as-heard
            continue
        # blank segment: name it from the next genuinely-skipped reference line
        cand = last_ref + 1
        if cand < m and cand not in matched_refs:
            seg = dict(s)
            seg["text"] = ref_lines[cand]
            seg["review"] = True
            out.append(seg)
            last_ref = cand
            n_filled += 1
        # else: drop the empty segment

    logger.info(
        "[TEXT-CORRECT] %s corrected, %s blanks named from reference, %s segs out",
        n_swapped, n_filled, len(out),
    )
    return out


# Tunables for ad-lib relabeling.
_ADLIB_MIN_DUR_S = 10.0   # only inspect sufficiently long segments
_ADLIB_MIN_PACE_S = 2.0   # s/word above which a long segment is a sustained vocal
_ADLIB_CHUNK_S = 3.5      # target length of each relabeled "Uh" line


def relabel_long_adlibs(segs: list[dict],
                        *, min_dur: float = _ADLIB_MIN_DUR_S,
                        min_pace: float = _ADLIB_MIN_PACE_S,
                        chunk: float = _ADLIB_CHUNK_S) -> list[dict]:
    """Relabel sustained-vocalisation segments as "Uh".

    Whisper forces WORDS onto wordless ad-libs: a 21 s "uh uh uh" block comes
    back as e.g. "¿Para qué? ¿Para qué?" — which text-correct then matches to a
    chorus line, so the ad-lib renders as a (wrong) lyric. Signal: a LONG
    segment (>= `min_dur`) with very FEW words for its length (>= `min_pace`
    s/word) is a sustained vocalisation, not a sung lyric. Real lyrics — even
    long lines — run well under 1.5 s/word, so they're untouched. We relabel
    such a segment to "Uh" lines split into ~`chunk`-second pieces (matching how
    ROTOR shows the block), flagged `review`.

    Run this on the raw transcription BEFORE text-correct so the relabeled "Uh"
    lines have no reference match and survive verbatim. Returns a NEW list.
    """
    out: list[dict] = []
    n_relabeled = 0
    for s in segs:
        text = (s.get("text") or "").strip()
        st = float(s.get("start", 0.0))
        en = float(s.get("end", st))
        dur = en - st
        nwords = len(text.split()) or 1
        if dur >= min_dur and dur / nwords >= min_pace:
            n = max(1, round(dur / chunk))
            step = dur / n
            for k in range(n):
                out.append({"start": round(st + k * step, 3),
                            "end": round(st + (k + 1) * step, 3),
                            "text": "Uh, uh, uh", "review": True})
            n_relabeled += 1
        else:
            out.append(dict(s))
    if n_relabeled:
        logger.info("[ADLIB] relabeled %s sustained-vocal segment(s) as 'Uh'", n_relabeled)
    return out
