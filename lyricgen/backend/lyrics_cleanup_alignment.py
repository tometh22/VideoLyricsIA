"""Re-align canonical text after Gemini cleanup expanded it beyond lrclib synced.

WHY
---
`pipeline._gemini_cleanup_lyrics` (PR #371) can expand the canonical text
when lrclib was missing repetitions or the outro. For "638" (Viejas Locas)
lrclib brought 19 lines + 19 LRC timestamps; cleanup expanded to 26 lines
(recovered "Y empecé / Y empecé", "Y pensé / Y pensé", outro repeated).

Until now the downstream path was:

  cleanup → 26-line canonical → whisperX with canonical as hint
          → whisperx_reconcile (drift abort, 26 > whisperX wordstamps)
          → forced_align_lyrics (cureau)
          → wordstamps_to_segments with 26 lines vs ~95 words
          → 7 extra lines clampean al final del stream → PILE-UP at single TS

Incident "638" 2026-05-26: 5 lines all stuck at exactly 1:15.5, plus
out-of-order timestamps and 27s/51s silent gaps. See
`/Users/tomi/.claude/plans/image-24-la-letra-robust-moonbeam.md`.

CONTRACT
--------
`align_cleaned_against_synced(cleaned_lines, synced_pairs, audio_dur)`

Pure function. Returns list of segments `[{"start", "end", "text"}]`
covering every line of `cleaned_lines` in cleanup's original order:

  - Lines that fuzzy-match an LRC pair → keep the LRC start time
    (preserves the timing we already had right).
  - Lines that don't match (cleanup additions) → linearly interpolate
    between the two anchored neighbours.
  - End times = next start - GAP_S (or audio_dur for the last line).

Returns None when input shape makes alignment impossible (no synced pairs,
or cleaned/synced disagree so drastically that interpolation cannot
preserve monotonicity).

Gating in caller (`main.py`): only invoke when cleanup expanded the canonical
beyond what lrclib synced has. Otherwise the existing path is fine.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("genly.cleanup_align")

# Fuzzy-match threshold: tolerates typo fixes from cleanup. First draft
# used 0.65 but that misses "y vi un corazón que decía mía mor" vs
# "Y vi un corazón que decía: 'Mi amor'," — Jaccard ≈ 0.60 because the
# typo fix changes 2 of 10 tokens. Below ~0.50 we start matching unrelated
# lines that happen to share connectives. 0.55 picks the right side of
# the trade-off for community-transcribed Spanish lyrics.
DEFAULT_MATCH_THRESHOLD = 0.55

# Min gap between end of a segment and start of the next, in seconds.
# Matches the convention used by _lrc_to_segments (pipeline.py:2613).
GAP_S = 0.05

# Min duration of an interpolated segment. Below this we round up so the
# editor and the renderer don't degenerate (very-short subtitles flash).
MIN_SEG_DUR_S = 0.6

# Reserved end-time padding when a line is the very last and we have to
# extrapolate beyond the last synced anchor.
TAIL_PADDING_S = 2.5


_WORD_RE = re.compile(r"[a-záéíóúüñA-ZÁÉÍÓÚÜÑ0-9]+")


def _tokens(text: str) -> set[str]:
    """Lowercased word tokens; punctuation stripped. Empty set if no text."""
    if not text:
        return set()
    return {m.group(0).lower() for m in _WORD_RE.finditer(text)}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = a & b
    union = a | b
    return len(inter) / len(union) if union else 0.0


@dataclass
class _Anchor:
    """Internal: a cleaned-line index with its assigned start time."""
    idx: int          # index into cleaned_lines
    start: float      # seconds


def _match_cleaned_to_synced(
    cleaned_lines: list[str],
    synced_pairs: list[tuple[float, str]],
    threshold: float = DEFAULT_MATCH_THRESHOLD,
) -> list[Optional[float]]:
    """For each cleaned line, return the synced pair's time it matches, or
    None if unmatched.

    Uses dynamic programming for **globally optimal monotonic** assignment.
    The earlier greedy version (one-pass with `next_pair = best_j + 1`)
    failed on "638" (incidente 2026-05-26): cleaned line "llamame al
    6380465" lexically matched the late synced pair [02:19.43] "llamame al"
    with high Jaccard (0.67), advanced the cursor past pair[9-17], and
    starved every subsequent cleaned line (verse 2 + outro) of anchors —
    they all got interpolated into the tail and piled up around 2:23.

    DP formulation:
      dp[i][j] = max total score across all monotonic alignments using
                 cleaned[0..i-1] and synced[0..j-1].
      Transitions:
        match  : dp[i-1][j-1] + score(i-1, j-1)   if score ≥ threshold
        skip-c : dp[i-1][j]      (cleaned[i-1] is new, no anchor needed)
        skip-s : dp[i][j-1]      (synced[j-1] never sung, e.g. cleanup
                                  consolidated two lrclib lines into one)

    Tie-break: when two monotonic alignments give the same total Jaccard
    sum (e.g. a chorus repeated 4 times in cleanup vs once in synced),
    prefer matching the **earliest** cleaned occurrence to the synced
    anchor. The later repetitions then interpolate to the tail.

    Implementation: each match score gets a tiny `EARLIEST_BIAS_EPSILON`
    boost inversely proportional to `i` (the cleaned index). For two
    otherwise-equal matches, this makes the earlier-i match strictly
    higher-scoring so DP picks it deterministically. The bias is small
    enough (1e-6) that it never overrides a real Jaccard difference, but
    large enough to break exact ties. Without this, `>=` on the DP match
    transition was not sufficient because traceback walks backwards from
    (n, m) and selects whichever cell holds `match` action last — that
    cell tends to be the LATEST i, not the earliest.

    No position bonus: the previous draft added a `pos_ratio` term to nudge
    matches toward equal positional fractions. For "638" that backfired —
    when chorus lines repeat, the second occurrence has a higher i/n ratio
    that happens to be closer to the synced anchor's j/m ratio, so the
    bonus made the DP prefer matching the REPETITION instead of the first
    instance. Pure Jaccard + earliest tie-break gives the right answer.

    Runtime: O(n*m). For typical "638" (n=27, m=19) that's ~500 cells, < 1ms.
    """
    n = len(cleaned_lines)
    m = len(synced_pairs)
    if n == 0 or m == 0:
        return [None] * n

    cleaned_toks = [_tokens(line) for line in cleaned_lines]
    pair_toks = [_tokens(text) for _, text in synced_pairs]

    # Tie-breaker: tiny bias toward earlier cleaned indices. Six orders of
    # magnitude smaller than any real Jaccard differential, so it only
    # decides exact ties — never overrides a real match-quality difference.
    EARLIEST_BIAS_EPSILON = 1e-6

    def score(i: int, j: int) -> float:
        lex = _jaccard(cleaned_toks[i], pair_toks[j])
        if lex < threshold:
            return 0.0
        # `(n - i)` gives an inverted bias: i=0 gets max boost, i=n-1 gets
        # minimum. So earlier i → higher score → DP prefers it.
        return lex + EARLIEST_BIAS_EPSILON * (n - i)

    # DP table + parent pointers for traceback.
    dp = [[0.0] * (m + 1) for _ in range(n + 1)]
    # parent[i][j] = ('match', 'skip_c', 'skip_s')
    parent = [[None] * (m + 1) for _ in range(n + 1)]

    for i in range(1, n + 1):
        for j in range(1, m + 1):
            # Default: skip cleaned[i-1] (no match).
            best = dp[i - 1][j]
            best_action = "skip_c"

            # Try skipping synced[j-1] (no cleaned line matches it).
            v = dp[i][j - 1]
            if v > best:
                best = v
                best_action = "skip_s"

            # Try matching cleaned[i-1] to synced[j-1]. Strict `>` is fine
            # now because the earliest-bias epsilon already breaks ties
            # deterministically inside `score`.
            s = score(i - 1, j - 1)
            if s > 0:
                v = dp[i - 1][j - 1] + s
                if v > best:
                    best = v
                    best_action = "match"

            dp[i][j] = best
            parent[i][j] = best_action

    # Traceback to recover the assignment.
    assigned: list[Optional[float]] = [None] * n
    i, j = n, m
    while i > 0 and j > 0:
        action = parent[i][j]
        if action == "match":
            assigned[i - 1] = synced_pairs[j - 1][0]
            i -= 1
            j -= 1
        elif action == "skip_c":
            i -= 1
        else:  # skip_s
            j -= 1

    return assigned


def _build_anchors(
    cleaned_lines: list[str],
    assigned_times: list[Optional[float]],
    audio_dur: float,
) -> list[_Anchor]:
    """Convert the parallel assigned_times array into a sparse anchor list.

    Skips anchors that would break monotonicity vs the previous accepted
    one. This is a defensive step — `_match_cleaned_to_synced` is greedy
    monotonic but a bad fuzzy hit could still produce a backwards anchor.
    """
    anchors: list[_Anchor] = []
    last_t = -1.0
    for i, t in enumerate(assigned_times):
        if t is None:
            continue
        # Discard backwards anchors. They almost never happen in practice
        # but if they do, dropping them keeps interpolation sane.
        if t <= last_t:
            logger.debug(
                "[CLEANUP-ALIGN] skipping non-monotonic anchor at cleaned[%d] "
                "t=%.2f (prev anchor t=%.2f)", i, t, last_t,
            )
            continue
        if t > audio_dur + 1.0:
            # Anchor beyond audio duration: synced LRC ran past the audio.
            # Drop it; we can't trust the surrounding interpolation either.
            logger.debug(
                "[CLEANUP-ALIGN] skipping anchor past audio_dur at cleaned[%d] "
                "t=%.2f (audio_dur=%.2f)", i, t, audio_dur,
            )
            continue
        anchors.append(_Anchor(idx=i, start=t))
        last_t = t
    return anchors


def _interpolate_starts(
    n_lines: int,
    anchors: list[_Anchor],
    audio_dur: float,
) -> list[float]:
    """Fill a length-`n_lines` start-time array using the sparse anchors.

    Lines at anchor positions get the anchor's start time. Lines between
    two anchors get linearly-interpolated starts. Lines before the first
    anchor get pushed back uniformly; lines after the last anchor get
    spread to audio_dur with TAIL_PADDING_S margin.
    """
    starts = [0.0] * n_lines

    if not anchors:
        # No anchors at all: uniform distribution from 0 to audio_dur.
        if n_lines > 0:
            for i in range(n_lines):
                starts[i] = (i / n_lines) * audio_dur
        return starts

    # Lines before the first anchor.
    first = anchors[0]
    if first.idx > 0:
        head_start = max(0.0, first.start - first.idx * 2.0)
        per = (first.start - head_start) / first.idx if first.idx > 0 else 0.0
        for i in range(first.idx):
            starts[i] = head_start + i * per

    # Lines at and between anchors.
    for k, anc in enumerate(anchors):
        starts[anc.idx] = anc.start
        if k + 1 < len(anchors):
            nxt = anchors[k + 1]
            gap_lines = nxt.idx - anc.idx - 1
            if gap_lines > 0:
                span = nxt.start - anc.start
                step = span / (gap_lines + 1)
                for j in range(1, gap_lines + 1):
                    starts[anc.idx + j] = anc.start + j * step

    # Lines after the last anchor.
    last = anchors[-1]
    tail_lines = n_lines - last.idx - 1
    if tail_lines > 0:
        tail_end = max(last.start + (tail_lines + 1) * MIN_SEG_DUR_S,
                       audio_dur - TAIL_PADDING_S)
        tail_end = min(tail_end, audio_dur)
        span = max(0.0, tail_end - last.start)
        step = span / (tail_lines + 1) if tail_lines + 1 > 0 else 0.0
        for j in range(1, tail_lines + 1):
            starts[last.idx + j] = last.start + j * step

    return starts


def _starts_to_segments(
    cleaned_lines: list[str],
    starts: list[float],
    audio_dur: float,
) -> list[dict]:
    """Pair each start with an end and the line text. End = next start -
    GAP_S, or audio_dur for the last line. Enforces MIN_SEG_DUR_S floor."""
    n = len(cleaned_lines)
    out: list[dict] = []
    for i, line in enumerate(cleaned_lines):
        start = max(0.0, starts[i])
        if i + 1 < n:
            end = max(start + MIN_SEG_DUR_S, starts[i + 1] - GAP_S)
        else:
            end = max(start + MIN_SEG_DUR_S, audio_dur - 0.0)
        end = min(end, audio_dur)
        out.append({"start": float(start), "end": float(end), "text": line})
    return out


def align_cleaned_against_synced(
    cleaned_lines: list[str],
    synced_pairs: list[tuple[float, str]],
    audio_dur: float,
    *,
    match_threshold: float = DEFAULT_MATCH_THRESHOLD,
) -> Optional[list[dict]]:
    """Re-time `cleaned_lines` using `synced_pairs` as anchors.

    Args:
        cleaned_lines: lyric lines in the order produced by cleanup. Must be
            non-empty; lines must be stripped (no empty entries).
        synced_pairs: parsed LRC pairs `[(seconds, text), ...]` from lrclib
            synced lyrics. Must be sorted by time ascending.
        audio_dur: real duration of the audio file in seconds. Must be > 0.
        match_threshold: minimum Jaccard token-overlap to accept a fuzzy
            match between a cleaned line and a synced pair.

    Returns:
        List of `n` segments `[{"start", "end", "text"}]` where
        `n = len(cleaned_lines)`, ordered by start time, monotonic,
        no overlaps. Returns None when:
          - inputs are empty
          - no synced pairs are usable (all out of range, all non-monotonic)
          - the resulting starts cannot be made monotonic
    """
    if not cleaned_lines or not synced_pairs or audio_dur <= 0:
        return None

    cleaned = [ln.strip() for ln in cleaned_lines if ln and ln.strip()]
    if not cleaned:
        return None

    assigned = _match_cleaned_to_synced(cleaned, synced_pairs, match_threshold)
    anchors = _build_anchors(cleaned, assigned, audio_dur)

    n_anchors = len(anchors)
    n_cleaned = len(cleaned)
    n_matched = sum(1 for t in assigned if t is not None)
    logger.info(
        "[CLEANUP-ALIGN] cleaned=%d synced=%d matched=%d anchors=%d audio_dur=%.1f",
        n_cleaned, len(synced_pairs), n_matched, n_anchors, audio_dur,
    )

    if n_anchors == 0:
        # No anchors at all — better to fall back to caller's existing path
        # (whisperX/FA) than to lay down a uniform distribution that pretends
        # to be aligned.
        logger.info("[CLEANUP-ALIGN] no usable anchors; returning None")
        return None

    starts = _interpolate_starts(n_cleaned, anchors, audio_dur)

    # Defensive: enforce monotonicity. If interpolation produced non-strictly
    # increasing starts (only possible if anchors were too dense), bump the
    # offending starts by MIN_SEG_DUR_S.
    for i in range(1, len(starts)):
        if starts[i] <= starts[i - 1]:
            starts[i] = starts[i - 1] + MIN_SEG_DUR_S

    segments = _starts_to_segments(cleaned, starts, audio_dur)

    # Final sanity: confirm monotonicity holds after end-time computation.
    for i in range(1, len(segments)):
        if segments[i]["start"] < segments[i - 1]["start"]:
            logger.warning(
                "[CLEANUP-ALIGN] non-monotonic after build at i=%d "
                "(%.2f < %.2f); returning None to fall back",
                i, segments[i]["start"], segments[i - 1]["start"],
            )
            return None

    return segments


__all__ = [
    "align_cleaned_against_synced",
    "DEFAULT_MATCH_THRESHOLD",
]
