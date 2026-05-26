"""Post-processing helpers for the transcription pipeline output.

`normalize_words` is the contract that protects Bug B (line 3741 of
main.py used to strip `words` UNCONDITIONALLY before persisting, even
when the words carried the per-word `score` from forced alignment that
PR-D needs for karaoke + confidence highlighting). The fix is
conditional: preserve when score is present (FA aligner output),
strip when not (Whisper-1 raw — redundant with line-level timing and
~30% JSON bloat for nothing).
"""
from __future__ import annotations


def normalize_words(segments: list) -> list:
    """Apply the per-word stripping policy on a list of segment dicts.

    Rules:
    - A segment without a `words` key passes through unchanged.
    - A segment whose `words` list contains AT LEAST one entry with a
      non-None `score` field is treated as forced-aligner output — keep
      the `words` intact (each carries start/end + score; karaoke +
      confidence-highlight read them).
    - A segment whose `words` list has no `score` anywhere is treated as
      Whisper-1 raw output — strip the `words` key. The line-level
      start/end already covers everything the editor needs from that
      path, and the per-word stamps add tens of KB per song to the
      segments_json with no visible win.

    Bug B incident (2026-05-24): the previous unconditional strip in
    `main.py:3741` killed FA wordstamps when the flow happened to land
    on the tail return path — anyone who saw Karaoke working in QA
    didn't notice because that path wasn't exercised. The QA full run
    finally hit it on Cosas Mías auto-recovery and we lost karaoke +
    confidence on jobs that should have had both.

    Pure function. Doesn't mutate the input. Unit-testable without
    pytest fixtures, replicate mocks, or an event loop.
    """
    out = []
    for s in segments:
        words = s.get("words")
        if not words:
            out.append(s)
            continue
        if any(w.get("score") is not None for w in words):
            out.append(s)
        else:
            out.append({k: v for k, v in s.items() if k != "words"})
    return out


def filter_rescue_candidates(
    wx_segs: list,
    *,
    start_s: float,
    end_s: float,
    buffer_s: float = 1.0,
) -> list:
    """Pick the subset of whisperX segments that fit ENTIRELY inside a time
    window [start_s + buffer, end_s - buffer]. Used to rescue lines in
    gaps where forced-align emitted nothing (because lrclib's lyrics
    didn't include them).

    INCIDENT 2026-05-25 (Bug A of the "Legalícenla" incident): lrclib's
    community-curated lyrics OMIT chorus repeats — both intro chorus and
    sometimes chorus instances in the middle of the song. Forced-align
    honours the text it gets, so wherever lrclib has a gap, the FA result
    has a gap too. The fix uses the warm whisperX result to rescue lines
    that wx detected in those windows. This function is the pure
    data-transform step; the integration in main.py awaits the warm task,
    calls this for each gap window, runs hallucination + repetition
    filters, and merges into fa_segs.

    REV 2026-05-25 (post-PR #307): originally this only filtered the INTRO
    window [0, first_fa_start - buffer]. The body of "Legalícenla" had
    25-second gaps where lrclib omitted entire chorus repeats — same bug,
    different position. Generalised to any [start_s, end_s] window so we
    can sweep every gap.

    Args:
      wx_segs: list of whisperX segment dicts with `start`/`end` keys.
      start_s: lower bound of the rescue window. Rescued segments must
        start AFTER this plus `buffer_s`.
      end_s: upper bound of the rescue window. Rescued segments must end
        BEFORE this minus `buffer_s`.
      buffer_s: time gap (seconds) on each end so the rescued line doesn't
        touch the FA segments bracketing the gap.

    Returns:
      A new list (subset of wx_segs) of segments that fit entirely in
      [start_s + buffer_s, end_s - buffer_s]. Empty list if no candidates
      or input is empty. Doesn't mutate `wx_segs`.
    """
    if not wx_segs:
        return []
    lo = start_s + buffer_s
    hi = end_s - buffer_s
    if hi <= lo:
        return []
    return [
        dict(s) for s in wx_segs
        if float(s.get("start") or 0) >= lo
        and float(s.get("end") or 0) <= hi
    ]


# Backwards-compat wrapper: the old name only handled the intro case.
def filter_intro_rescue_candidates(
    wx_segs: list,
    *,
    first_main_start_s: float,
    buffer_s: float = 1.0,
) -> list:
    """DEPRECATED: kept for any callers still using the intro-only API.
    Prefer `filter_rescue_candidates(start_s=0, end_s=first_main_start_s)`."""
    return filter_rescue_candidates(
        wx_segs,
        start_s=0.0,
        end_s=first_main_start_s,
        buffer_s=buffer_s,
    )


def _norm_tokens(text: str) -> set:
    """Lowercase + strip Latin accents + split into tokens. Shared helper
    for both the repetitive-segments check and the reference-text match."""
    import unicodedata
    norm = unicodedata.normalize("NFKD", (text or "").lower())
    norm = "".join(c for c in norm if not unicodedata.combining(c))
    return {w for w in norm.split() if w}


def is_suspiciously_repetitive(
    segments: list,
    *,
    min_count: int = 3,
    similarity: float = 0.7,
    reference_text: str | None = None,
) -> bool:
    """DEPRECATED (2026-05-25) — legacy FA-primary pipeline only.

    The audio-as-truth pipeline (default `AUDIO_AS_TRUTH=1`) does NOT call
    this helper. It exists to catch FA's cramming-then-reject pattern that
    only appears when forced_align is the timing source. With whisperX as the
    truth source + phonetic reconcile, the conditions that triggered the
    "Le realizan la × 3 rejected, intro chorus lost" failure mode cannot
    occur — the wordstamps carry their own acoustic anchors.

    Slated for removal once the legacy FA path is deleted (see main.py
    "LEGACY FA-primary pipeline" banner). Do NOT add new callers.

    Original docstring follows:

    Heuristic: does this batch look like a stuck-phoneme hallucination?

    Returns True only when ALL of these hold:
    1. ≥ `min_count` segments in the batch
    2. ≥ 50 % of segment pairs share ≥ `similarity` tokens (Jaccard)
    3. The repeated tokens DO NOT appear in `reference_text` (when given)

    INCIDENT 2026-05-25 #1 (PR #308): whisperX rescued
    ["Le realizan la"] × 4 across the intro of "Legalícenla". Actual
    lyric is "Legalícenla" — whisperX mis-heard the phonemes and emitted
    garbage. The repetition was the smoking gun and we rejected.

    INCIDENT 2026-05-25 #2 (this fix): the SAME guard then rejected the
    LEGITIMATE intro chorus of "Legalícenla" itself — "Legalícenla /
    Legalícenla / Legalícenla / Oh-oh-oh" repeats verbatim in lrclib /
    Genius lyrics. The guard treated chorus-as-chorus as hallucination
    and emptied the rescue, leaving the timestamps for the intro lost
    and the first segment landing at 0:45 (verse 1) instead of 0:17.
    Comparing to production (commits behind, no guard): timing was
    perfect at 0:17 even with bad text. We regressed timing for text
    correctness.

    Fix: the guard now consults the reference lyrics. If the repeated
    tokens APPEAR in the reference text, the repetition is a legitimate
    chorus repeat — NOT a hallucination. Only the no-reference case
    (true bare-Whisper output with no ground truth) still rejects.

    Caller passes `reference_text` (lrclib/Genius/Gemini plain text)
    whenever it has one — that's > 95 % of jobs.
    """
    if not segments or len(segments) < min_count:
        return False

    # Pairwise Jaccard on segment token sets.
    token_sets = [_norm_tokens(s.get("text") or "") for s in segments]
    similar_count = 0
    similar_token_union: set = set()
    for i in range(len(token_sets)):
        for j in range(i + 1, len(token_sets)):
            a, b = token_sets[i], token_sets[j]
            if not a or not b:
                continue
            jaccard = len(a & b) / max(1, len(a | b))
            if jaccard >= similarity:
                similar_count += 1
                similar_token_union |= (a & b)

    n = len(segments)
    total_pairs = n * (n - 1) // 2
    if total_pairs == 0:
        return False
    ratio = similar_count / total_pairs
    if ratio < 0.5:
        return False        # not repetitive enough to flag

    # At this point the batch IS structurally repetitive. The question
    # is: is it a hallucination or a real chorus? Consult reference.
    if reference_text and similar_token_union:
        ref_tokens = _norm_tokens(reference_text)
        # Fraction of the REPEATED tokens that show up in the canonical
        # lyrics. > 50 % means most of what whisperX heard repeatedly is
        # actually written in the lyrics — chorus behaviour, not
        # phoneme-stuck hallucination. Strict > 0.5 so a single match
        # against 2-token noise doesn't rescue garbage.
        overlap = similar_token_union & ref_tokens
        ratio_in_ref = len(overlap) / len(similar_token_union)
        if ratio_in_ref > 0.5:
            return False    # legitimate chorus

    # No reference, or reference doesn't contain these tokens → looks
    # like genuine stuck-phoneme hallucination.
    return True


def dedup_collisions(segments: list, *, epsilon_s: float = 0.35) -> list:
    """Merge near-identical duplicates that forced_align/reconcile sometimes
    emits when the lyrics text has repeated chorus lines ("Legalícenla /
    Legalícenla / Oh-oh-oh"). Two segments collide when they share (a) the
    same start within `epsilon_s` (default 350 ms — increased from 100 ms
    after the 2026-05-25 review showed duplicates at 0:45.4 / 0:45.7,
    300 ms apart, which the old threshold missed) and (b) the same
    case-insensitive text. The first occurrence wins; its end is extended
    to the max of the group; the rest are dropped.

    300 ms is below the minimum singing rate for a 4-syllable word like
    "Legalícenla" (~600 ms even at allegro tempo), so two starts that close
    with identical text cannot both be legitimate vocal events — one is
    an aligner artefact.

    INCIDENT 2026-05-25: forced_align on songs whose lrclib lyrics include
    repeated chorus lines occasionally assigned all the repetitions to a
    single time bucket (~50-100 ms apart). The frontend was patched to show
    these as Gantt-style overlap lanes, but the root cause is the aligner
    emitting collisions in the first place. We dedup at the chokepoint so
    every emit path benefits (FA, whisperX_reconciled, whisper_recover…).

    DELIBERATELY does NOT merge segments that share `start` but differ in
    `text` — those are legitimate co-occurring lines (chorus harmony, two
    voices singing different words at once). The frontend handles those
    correctly via stack-rendering.

    Pure function. Doesn't mutate inputs. Returns a new list of new dicts.
    """
    if not segments or len(segments) < 2:
        return list(segments) if segments else []
    out: list = []
    for s in sorted(segments, key=lambda x: float(x.get("start") or 0)):
        if not out:
            out.append(dict(s))
            continue
        prev = out[-1]
        same_text = (
            (s.get("text") or "").strip().lower()
            == (prev.get("text") or "").strip().lower()
        )
        close_start = abs(
            float(s.get("start") or 0) - float(prev.get("start") or 0)
        ) < epsilon_s
        if same_text and close_start:
            prev["end"] = max(
                float(prev.get("end") or 0),
                float(s.get("end") or 0),
            )
            continue
        out.append(dict(s))
    return out
