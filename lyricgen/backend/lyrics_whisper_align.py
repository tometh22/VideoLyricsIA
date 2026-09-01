"""World-class alignment for cleanup-expanded canonicals via Whisper-1
word-level timestamps + DP alignment.

WHY (the audit trail)
---------------------
After `_gemini_cleanup_lyrics` adds repetitions/puentes to the canonical
(e.g. "638" goes from 19 → 27 lines with "Y empecé/Y empecé" puentes +
outro repeated), every existing downstream path falls short:

  - `whisperx_reconcile` aborts on drift (too many canonical lines vs
    whisperX wordstamps).
  - `forced_align_lyrics` clamps unmatchable extras to end of word
    stream → pile-up at a single timestamp (incident "638" 2026-05-26:
    5 lines stuck at 1:15.5).
  - `align_cleaned_against_synced` uses lrclib synced as anchors +
    linear interpolation → mediocre on long instrumentals (the 27 s and
    51 s gaps in "638" got "Y empecé/Y empecé" lines placed at uniform
    spacings, none of which match where the singer actually sings them).

THIS MODULE — call Whisper-1 directly on the audio with the cleaned
canonical as `initial_prompt`, request word-level timestamps, then DP-
align cleaned tokens against whisper words. Per cleaned line: the start
time of its first matched token IS the timestamp.

Empirically (probe 2026-05-26 vs "638"):

  | Approach                      | anchored | avg Δ vs lrclib | max Δ |
  |-------------------------------|----------|-----------------|-------|
  | Gemini-as-aligner (JSON ts)   | 0/27     | 25.7 s          | 77 s  |
  | align_cleaned_against_synced  | varies   | varies          | varies|
  | **Whisper-1 word-level + DP** | **27/27**| **2.6 s**       | 5.6 s |

Whisper-1 is the only OpenAI transcription model that returns word-
level timestamps. gpt-4o-transcribe / gpt-4o-mini-transcribe reject
`response_format=verbose_json` (confirmed 2026-05-26 via API).
WhisperX via Replicate would give similar results but the reconcile
step is exactly what we're working around here.

CONTRACT
--------
`whisper_word_align(audio_path, cleaned_lines, language=None) -> list[Segment] | None`

Pure-ish (does I/O for the API call). Returns segments
`[{"start", "end", "text"}]` covering every line of `cleaned_lines`,
or `None` when:
  - Whisper API call failed (network, rate limit, key missing)
  - Whisper returned no word stamps
  - DP align produced < 30 % anchored lines (likely Whisper transcribed
    something very different from cleaned — bail to caller's fallback)
  - audio compression failed for size > 25 MB

The caller (`main.py`) is responsible for:
  - Gating on `OPENAI_API_KEY` available
  - Falling back to `align_cleaned_against_synced` or further when this
    returns None
  - Cleaning up the compressed audio temp file
"""
from __future__ import annotations

import logging
import os
import re
from typing import Optional

logger = logging.getLogger("genly.whisper_align")

# Min gap between end-of-segment and start-of-next, seconds. Matches
# `_lrc_to_segments` convention.
GAP_S = 0.05

# Min duration of a segment. Below this the editor and renderer
# degenerate (subtitles flash).
MIN_SEG_DUR_S = 0.6

# Min ratio of cleaned lines that must be anchored to whisper words for
# us to trust the alignment. Below this Whisper probably transcribed
# something very different from cleaned (operator gave wrong song /
# karaoke / instrumental); bail to caller.
MIN_ANCHOR_RATIO = 0.30

# Whisper API `prompt` is capped at ~244 tokens. We truncate the cleaned
# canonical to 200 words to leave headroom. The prompt biases vocabulary
# (accents, proper nouns, numbers) but doesn't need the whole song.
PROMPT_MAX_WORDS = 200


_TOKEN_RE = re.compile(r"[a-záéíóúüñA-ZÁÉÍÓÚÜÑ0-9]+")


def _tokens_with_line(cleaned_lines: list[str]) -> list[tuple[int, str]]:
    """Tokenize cleaned lines into `(line_idx, normalized_token)` pairs."""
    out: list[tuple[int, str]] = []
    for li, line in enumerate(cleaned_lines):
        for m in _TOKEN_RE.finditer(line):
            out.append((li, m.group(0).lower()))
    return out


def _normalize_whisper_word(word: str) -> str:
    return re.sub(r"[^\w]", "", word).lower()


def _lev_similarity(a: str, b: str) -> float:
    """Crude similarity: 1.0 if equal, 0.5 if one prefixes the other
    (catches conjugation tails like "decía"/"decías"), else 0.0.

    For our DP this is sufficient — Whisper's transcription has 90%+
    word-perfect overlap with cleaned canonical in Spanish lyrics, so
    we don't need real Levenshtein distance. The 0.5 prefix case is the
    rare savior for accent-stripped comparisons that didn't equal.
    """
    if a == b:
        return 1.0
    if a and b and (a.startswith(b) or b.startswith(a)) and min(len(a), len(b)) >= 3:
        return 0.5
    return 0.0


def _dp_align_tokens(
    cleaned_tokens: list[tuple[int, str]],
    whisper_words: list[dict],
) -> list[int]:
    """Needleman-Wunsch alignment of cleaned tokens against whisper words.

    Args:
        cleaned_tokens: `(line_idx, normalized_token)` from
            `_tokens_with_line`.
        whisper_words: each item must have `.word` (or `["word"]`) and
            `.start` (or `["start"]`).

    Returns:
        Parallel list of length `len(cleaned_tokens)`. Each entry is the
        index into `whisper_words` that matched, or -1 if unmatched.
    """
    n = len(cleaned_tokens)
    m = len(whisper_words)
    if n == 0 or m == 0:
        return [-1] * n

    # Normalize whisper words once.
    wtokens: list[str] = []
    for w in whisper_words:
        word = w.get("word") if isinstance(w, dict) else getattr(w, "word", "")
        wtokens.append(_normalize_whisper_word(word or ""))

    GAP_PENALTY = -0.3
    MISMATCH_PENALTY = -0.5

    # dp[i][j] = best alignment score using cleaned[:i] and whisper[:j].
    dp = [[0.0] * (m + 1) for _ in range(n + 1)]
    # parent: action that produced dp[i][j].
    parent: list[list[Optional[str]]] = [[None] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        dp[i][0] = i * GAP_PENALTY
        parent[i][0] = "gap_c"
    for j in range(1, m + 1):
        dp[0][j] = j * GAP_PENALTY
        parent[0][j] = "gap_w"

    for i in range(1, n + 1):
        ctok = cleaned_tokens[i - 1][1]
        for j in range(1, m + 1):
            wtok = wtokens[j - 1]
            sim = _lev_similarity(ctok, wtok)
            # Diagonal (consume both): match if sim >= 0.5, else mismatch.
            if sim >= 0.5:
                best = dp[i - 1][j - 1] + sim
                best_p = "match"
            else:
                best = dp[i - 1][j - 1] + MISMATCH_PENALTY
                best_p = "mismatch"
            # Gap in whisper (skip cleaned token — it wasn't transcribed).
            v = dp[i - 1][j] + GAP_PENALTY
            if v > best:
                best = v
                best_p = "gap_c"
            # Gap in cleaned (skip whisper word — heard something extra).
            v = dp[i][j - 1] + GAP_PENALTY
            if v > best:
                best = v
                best_p = "gap_w"
            dp[i][j] = best
            parent[i][j] = best_p

    # Traceback.
    cleaned_to_whisper = [-1] * n
    i, j = n, m
    while i > 0 or j > 0:
        if i == 0:
            j -= 1
            continue
        if j == 0:
            i -= 1
            continue
        p = parent[i][j]
        if p == "match":
            cleaned_to_whisper[i - 1] = j - 1
            i -= 1
            j -= 1
        elif p == "mismatch":
            # Consume both but don't trust the timestamp.
            i -= 1
            j -= 1
        elif p == "gap_c":
            i -= 1
        else:  # gap_w
            j -= 1

    return cleaned_to_whisper


# 2026-05-31 (Agus "Donde Estan Corazón" report).
#
# Fix C constant: any segment whose duration-per-character drops below
# this floor is logged as suspicious. Singers do 80-150 ms/char in
# steady delivery; <70 ms/char is essentially "this line was clamped
# tight against the next one and the end got truncated", which the
# operator reads as "first line starts before the vocal".
_TRUNCATED_MS_PER_CHAR_FLOOR = 70

# Fix A: per-word `score` placeholder for Whisper-1 word stamps. The
# downstream consumer that matters is `beat_snap._has_reliable_words`
# (threshold 0.5). Whisper-1 word onsets are accurate enough that we
# DO want beat_snap to skip these segments (snapping pulls them off
# the singer's actual entry). 0.6 sits comfortably above the
# threshold without overclaiming forced-align-grade confidence.
_WHISPER1_WORD_SCORE = 0.6


def _build_segments(
    cleaned_lines: list[str],
    cleaned_tokens: list[tuple[int, str]],
    cleaned_to_whisper: list[int],
    whisper_words: list[dict],
    audio_dur: float,
) -> list[dict]:
    """Per cleaned line, pick `start` from its first acoustically-matched
    token. Unanchored lines fill via linear interpolation between
    anchored neighbours.

    Each returned segment carries:
      - start, end, text  (always)
      - words[] from the Whisper-1 word stream that fall in [start,end)
        (added 2026-05-31 — before this, `_build_segments` discarded
        the 300+ word_dicts available, leaving the editor to
        linearly-interpolate the karaoke highlight from `text`. With
        sub-second segments that produced a noticeably wrong
        per-word cadence.)
    """
    n_lines = len(cleaned_lines)

    def _wstart(idx: int) -> float:
        w = whisper_words[idx]
        return float(w.get("start") if isinstance(w, dict) else w.start)

    def _wend(idx: int) -> float:
        w = whisper_words[idx]
        return float(w.get("end") if isinstance(w, dict) else w.end)

    def _wtext(idx: int) -> str:
        w = whisper_words[idx]
        raw = w.get("word") if isinstance(w, dict) else getattr(w, "word", None)
        return (raw or "").strip()

    # Per line: first matched cleaned-token's whisper start.
    line_start: list[Optional[float]] = [None] * n_lines
    for ti, (li, _) in enumerate(cleaned_tokens):
        if cleaned_to_whisper[ti] >= 0 and line_start[li] is None:
            line_start[li] = _wstart(cleaned_to_whisper[ti])

    anchored = [i for i, t in enumerate(line_start) if t is not None]
    if not anchored:
        return []

    # Interpolate head (before first anchor).
    #
    # NOTE 2026-05-31 evening: tried using `_wstart(0)` (first whisper
    # word's onset) instead of the fixed `first_t - first_ai*1.5`. On
    # the "Donde Estan Corazón" stem-isolated audio, the first whisper
    # word turned out to be an intro ad-lib/breath that Whisper picked
    # up at 8.25 s — but the actual first sung line started at 12 s.
    # The "smarter" head heuristic shifted seg[0] 3.8 s EARLY (worse
    # than the original 1.5 s/line constant). Reverted: the simpler
    # heuristic is empirically the lower-error choice on real audio.
    first_ai = anchored[0]
    if first_ai > 0:
        first_t = line_start[first_ai]
        head_t = max(0.0, first_t - first_ai * 1.5)
        for i in range(first_ai):
            line_start[i] = head_t + (first_t - head_t) * i / first_ai

    # Interpolate between anchors.
    for k in range(len(anchored) - 1):
        a, b = anchored[k], anchored[k + 1]
        if b - a > 1:
            t_a, t_b = line_start[a], line_start[b]
            for i in range(a + 1, b):
                line_start[i] = t_a + (t_b - t_a) * (i - a) / (b - a)

    # Interpolate tail (after last anchor).
    last_ai = anchored[-1]
    if last_ai < n_lines - 1:
        last_t = line_start[last_ai]
        tail_n = n_lines - last_ai
        # Cap at audio_dur with safety margin.
        tail_end = min(audio_dur, last_t + (n_lines - last_ai) * 3.0)
        for i in range(last_ai + 1, n_lines):
            line_start[i] = last_t + (tail_end - last_t) * (i - last_ai) / tail_n

    # Enforce strict monotonicity (defensive: interpolation may
    # marginally violate when anchors are very close).
    for i in range(1, n_lines):
        if line_start[i] <= line_start[i - 1]:
            line_start[i] = line_start[i - 1] + MIN_SEG_DUR_S

    # Build segments.
    segments: list[dict] = []
    for i, line in enumerate(cleaned_lines):
        start = max(0.0, float(line_start[i]))
        if i + 1 < n_lines:
            end = max(start + MIN_SEG_DUR_S, float(line_start[i + 1]) - GAP_S)
        else:
            end = max(start + MIN_SEG_DUR_S, audio_dur)
        end = min(end, audio_dur)
        segments.append({"start": start, "end": end, "text": line})

    # ─── Fix A (2026-05-31): persist per-segment word stamps ────────────
    # Bucket whisper words into each segment's [start, end] window
    # (greedy + monotonic — whisper words are already in time order so
    # one pass suffices). Each segment ends up with a `words` list that
    # the editor's karaoke highlight uses for accurate per-word timing.
    # When no words fall inside (rare: instrumental gap, segment built
    # purely from interpolation), the key is omitted — the editor's
    # fallback (linear interpolation of text within [start, end]) still
    # works.
    word_idx = 0
    for seg in segments:
        bucket: list[dict] = []
        seg_start = seg["start"]
        seg_end = seg["end"]
        while word_idx < len(whisper_words):
            ws = _wstart(word_idx)
            we = _wend(word_idx)
            # Done with this segment — peek next word, advance later.
            if ws >= seg_end:
                break
            # Include if there's any overlap with [seg_start, seg_end).
            if we > seg_start:
                bucket.append({
                    "word": _wtext(word_idx),
                    "start": ws,
                    "end": we,
                    "score": _WHISPER1_WORD_SCORE,
                })
            word_idx += 1
        if bucket:
            seg["words"] = bucket

    # ─── Fix C (2026-05-31): flag suspiciously truncated segments ───────
    # Log warning on segments whose duration-per-character ratio is
    # below the perceptual floor for sung delivery. Operator-visible
    # symptom: "the first line shows before the singer enters". This
    # logs ONE warning per offending segment so future jobs surface
    # the same root cause in Sentry breadcrumbs.
    for i, seg in enumerate(segments):
        chars = len((seg.get("text") or "").strip())
        if chars <= 0:
            continue
        dur = seg["end"] - seg["start"]
        ms_per_char = (1000.0 * dur) / chars
        if ms_per_char < _TRUNCATED_MS_PER_CHAR_FLOOR:
            logger.warning(
                "[WC] segment %d truncated: start=%.2fs end=%.2fs dur=%.2fs "
                "chars=%d → %.1f ms/char (floor=%d ms/char) text=%r",
                i, seg["start"], seg["end"], dur, chars, ms_per_char,
                _TRUNCATED_MS_PER_CHAR_FLOOR, (seg.get("text") or "")[:60],
            )

    return segments


def _map_provider_words(words) -> list[dict]:
    """Map the completed Whisper payload before any alignment declines."""
    mapped: list[dict] = []
    for word in words or []:
        try:
            if isinstance(word, dict):
                text = word.get("word")
                start = word.get("start")
                end = word.get("end")
            else:
                text = getattr(word, "word", None)
                start = getattr(word, "start", None)
                end = getattr(word, "end", None)
            mapped.append({"word": text, "start": start, "end": end})
        except Exception:
            continue
    return mapped


def whisper_word_align(
    audio_path: str,
    cleaned_lines: list[str],
    *,
    language: str | None = None,
    job_id: str | None = None,
) -> Optional[list[dict]]:
    """Align cleaned canonical lines against the audio using Whisper-1
    word-level timestamps.

    Args:
        audio_path: original audio file (any size; will be compressed if
            needed via `_compress_for_whisper`).
        cleaned_lines: list of lyric lines in cleanup's order (each line
            is one row of the editor).
        language: ISO-639-1 code for Whisper's language hint.
        job_id: optional, for provenance recording.

    Returns:
        Segments `[{"start", "end", "text"}]` (one per cleaned line), or
        `None` if alignment failed (caller falls back).
    """
    if not cleaned_lines:
        return None

    # Stripped, non-empty lines only.
    lines = [ln.strip() for ln in cleaned_lines if ln and ln.strip()]
    if not lines:
        return None

    # Gating: caller is responsible but defensive double-check.
    if not os.environ.get("OPENAI_API_KEY"):
        logger.info("[WHISPER-ALIGN] OPENAI_API_KEY not set; skipping")
        return None
    if not os.path.exists(audio_path):
        logger.warning("[WHISPER-ALIGN] audio not found: %s", audio_path)
        return None

    # Compress for Whisper API (25 MB ceiling). Reuses pipeline helper.
    from pipeline import _compress_for_whisper, _audio_duration
    api_path = None
    cleanup_compressed = False
    try:
        try:
            api_path = _compress_for_whisper(audio_path)
            cleanup_compressed = api_path != audio_path
        except RuntimeError as e:
            logger.warning("[WHISPER-ALIGN] compress failed: %s", e)
            return None

        # Whisper API call. Reuse openai SDK from pipeline namespace.
        try:
            import openai
        except ImportError:
            logger.warning("[WHISPER-ALIGN] openai SDK not installed")
            return None

        # Build initial_prompt: cleaned canonical truncated. Biases
        # vocabulary toward the song's words.
        prompt = " ".join(" ".join(lines).split()[:PROMPT_MAX_WORDS])

        client = openai.OpenAI()
        try:
            import time as _t
            t0 = _t.time()
            with open(api_path, "rb") as f:
                kwargs = {
                    "model": "whisper-1",
                    "file": f,
                    "prompt": prompt,
                    "response_format": "verbose_json",
                    "timestamp_granularities": ["word"],
                    "temperature": 0.0,
                }
                if language:
                    kwargs["language"] = language
                response = client.audio.transcriptions.create(**kwargs)
            elapsed = _t.time() - t0
        except Exception as e:
            logger.warning("[WHISPER-ALIGN] Whisper API failed: %s", str(e)[:200])
            return None

        words = getattr(response, "words", None) or []
        word_dicts = _map_provider_words(words)
        from recognition_provenance import record_completed
        record_completed(
            family="openai/whisper-1",
            events=word_dicts,
            kind="word_stream",
            view="alignment_audio",
            transformation="whisper_word_align_raw",
        )
        if not words:
            logger.warning("[WHISPER-ALIGN] response had no word stamps")
            return None

        # Provenance recording (best effort).
        if job_id:
            try:
                from provenance import record_ai_call
                rec = record_ai_call(
                    job_id=job_id,
                    step="whisper_align",
                    tool_name="whisper-1",
                    tool_provider="openai",
                    prompt=prompt[:500],
                    input_data_types=["audio_file", "cleaned_lyrics"],
                )
                if rec:
                    rec.finish(
                        response_summary=f"{len(words)}_word_stamps_in_{elapsed:.1f}s",
                    )
            except Exception:
                pass  # provenance is best-effort

        # DP align.
        cleaned_tokens = _tokens_with_line(lines)
        if not cleaned_tokens:
            logger.warning("[WHISPER-ALIGN] cleaned tokenization empty")
            return None

        cleaned_to_whisper = _dp_align_tokens(cleaned_tokens, word_dicts)
        anchored_lines = len({
            cleaned_tokens[i][0]
            for i, wi in enumerate(cleaned_to_whisper)
            if wi >= 0
        })
        anchor_ratio = anchored_lines / len(lines)
        logger.info(
            "[WHISPER-ALIGN] whisper=%d words, anchored=%d/%d lines (%.0f%%), %.1fs",
            len(words), anchored_lines, len(lines), anchor_ratio * 100, elapsed,
        )

        if anchor_ratio < MIN_ANCHOR_RATIO:
            logger.warning(
                "[WHISPER-ALIGN] anchor ratio %.2f below floor %.2f — "
                "likely wrong song or pure instrumental, bailing",
                anchor_ratio, MIN_ANCHOR_RATIO,
            )
            return None

        # Compute audio duration for tail.
        try:
            audio_dur = _audio_duration(audio_path) or float(word_dicts[-1]["end"])
        except Exception:
            audio_dur = float(word_dicts[-1]["end"]) if word_dicts else 0.0
        if audio_dur <= 0:
            return None

        segments = _build_segments(lines, cleaned_tokens, cleaned_to_whisper, word_dicts, audio_dur)
        if not segments:
            return None
        return segments

    finally:
        if cleanup_compressed and api_path and os.path.exists(api_path):
            try:
                os.unlink(api_path)
            except OSError:
                pass


# NOTE 2026-05-31 evening: `align_lines_to_words` + `flatten_whisperx_words`
# were tried as a "Fix D" — use whisperX forced-align words directly when
# `whisperx_reconcile` aborts, skipping the Whisper-1 re-call. Local E2E
# against the operator's "Donde Estan Corazón" stem-isolated audio showed
# the resulting line timings to be ~250 ms WORSE on average than the
# existing Whisper-1 path (mean |err| 642 vs 305 ms when compared against
# ground-truth onsets the operator timed by ear). Reverted; the
# Whisper-1 path stays as-is. The unit-test-only contracts for those two
# helpers were also removed.


__all__ = [
    "whisper_word_align",
    "MIN_ANCHOR_RATIO",
    "MIN_SEG_DUR_S",
    "GAP_S",
]
