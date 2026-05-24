"""Forced alignment of known lyrics to the user's audio (Rotor-grade timing).

WHY
---
Whisper's segment timing is loose (±500 ms) and merges/splits lines by
audio pauses; lrclib's community LRC is often misaligned to a given
audio file (different master, global offset, or fewer lines). When we
already know the lyrics (lrclib or Gemini), a *forced aligner* pins each
word to the ACTUAL audio at ±50 ms — the same technique Rotor's
"Transcribe & Sync" uses. We call a hosted model on Replicate so there's
no torch/GPU on the (small, CPU) workers.

CONTRACT
--------
- Behind `FORCED_ALIGNER_ENABLED` (default off) + `REPLICATE_API_TOKEN`.
- `forced_align_lyrics(audio_path, lyrics_text)` returns
  `[{"start","end","text"}]` aligned to the audio, or **None** on any
  failure / when disabled / when the result looks too thin — the caller
  must fall back to its existing path. It never raises.
- `wordstamps_to_segments` is pure (no network) and unit-testable.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile

logger = logging.getLogger("genly.forced_align")

# cureau/force-align-wordstamps — takes audio + transcript, returns
# {"wordstamps": [{"word","start","end"}, ...]}. Pinned version (spike
# 2026-05-21): whisperX/stable-ts under the hood, ~$0.007/song, ~75s.
_MODEL = (
    "cureau/force-align-wordstamps:"
    "44dedb84066ba1e00761f45c1003c5c19ed3b12ae9d42c1c1883ca4c016ffa85"
)

_TRUE = ("1", "true", "yes", "on", "y", "t")

_LRC_TS_RE = re.compile(r"^\s*(\[\d{1,2}:\d{2}(?:\.\d{1,3})?\]\s*)+")

# Error messages from the model / SDK / API that are NOT going to get
# better on retry. Three retries at 8s + 24s = 32 s of sleep + ~90 s per
# upload each (Mujer Amante baseline) = 5+ minutes wasted on a guaranteed
# failure. Match by lowercase substring against `str(exc)`.
#
# `Padding size should be less than the corresponding input dimension`
# (the [1,2,1] tensor bug) is the most common deterministic crash we've
# seen in the QA full run — Mujer Amante hit it three times in a row.
_NON_RETRYABLE_FRAGMENTS = (
    "padding size",
    "argument #4",
    "validationerror",
    "validation error",
    "invalid input",
    "unsupported audio",
    "transcript too long",
)


def _is_non_retryable(err: Exception) -> bool:
    """True if `err`'s message contains a known deterministic-failure
    fragment. Determines whether `_call_with_budget` should abort instead
    of sleeping + retrying."""
    msg = str(err).lower()
    return any(f in msg for f in _NON_RETRYABLE_FRAGMENTS)


def _call_with_budget(model: str, input_factory, *,
                       total_budget_s: float, backoff: list):
    """Call `replicate.run(model, input=input_factory())` with a global
    wall-clock budget and typed-error short-circuit. Returns the model
    output on success, or None on abort (budget exhausted, non-retryable
    error, or all attempts failed).

    `input_factory` is a zero-arg callable that returns a fresh `input`
    dict each call — the audio file handle is consumed on each upload,
    so we can't reuse a single dict across retries.

    Budget motivation (incident 2026-05-24 QA full run): the previous
    `for attempt in range(3): try replicate.run(...) except ...` loop
    had NO upper bound on total wall-clock time. When Replicate was
    degraded ("Server disconnected") each attempt's HTTP request took
    ~90 minutes to time out, so three attempts burned ~4.5 hours per
    job (observed: Intoxicados 16057s = 4h27min elapsed). The `total_
    budget_s` cap aborts ASAP when the cumulative time-on-the-wire
    exceeds 8 minutes by default — caller falls through to whisperX
    immediately instead of holding the job for hours.

    Backoff `[0, 8, 24]` keeps the previous behavior for transient
    flakes (cureau's intermittent `[1, 2, 0]` first-attempt error). The
    sleep is SKIPPED if it would push us past `deadline` — we'd rather
    abort and let the caller try a different path than burn a sleep
    quota on a doomed retry.
    """
    import replicate
    import time as _t
    deadline = _t.monotonic() + total_budget_s
    last_err = None
    for attempt, sleep_s in enumerate(backoff):
        if sleep_s > 0:
            if _t.monotonic() + sleep_s >= deadline:
                logger.warning("[FORCED] sleep %ss would exceed budget — aborting at attempt %s",
                               sleep_s, attempt + 1)
                break
            _t.sleep(sleep_s)
        if _t.monotonic() >= deadline:
            logger.warning("[FORCED] budget exhausted before attempt %s/%s",
                           attempt + 1, len(backoff))
            break
        try:
            return replicate.run(model, input=input_factory())
        except Exception as e:
            last_err = e
            if _is_non_retryable(e):
                logger.warning("[FORCED] non-retryable error on attempt %s (%s) — aborting",
                               attempt + 1, e)
                return None
            logger.warning("[FORCED] attempt %s/%s failed (%s)",
                           attempt + 1, len(backoff), e)
    logger.warning("[FORCED] exhausted attempts/budget — last_err=%s", last_err)
    return None


def is_enabled() -> bool:
    """On only when the flag is set AND a Replicate token is present."""
    flag = os.environ.get("FORCED_ALIGNER_ENABLED", "0").strip().lower() in _TRUE
    return flag and bool(os.environ.get("REPLICATE_API_TOKEN", "").strip())


def lrc_to_plain_text(synced: str | None) -> str:
    """Strip the `[mm:ss.xx]` timestamp prefixes from an LRC string,
    leaving just the lyric lines (used when lrclib has synced but no
    separate plain field)."""
    if not synced:
        return ""
    out = []
    for line in synced.splitlines():
        text = _LRC_TS_RE.sub("", line).strip()
        if text:
            out.append(text)
    return "\n".join(out)


def _word_dur(w: dict):
    try:
        d = float(w["end"]) - float(w["start"])
        return d if d > 0 else None
    except (TypeError, ValueError, KeyError):
        return None


def _norm(s: str) -> str:
    """Lowercase + strip combining diacritics + drop non-alphanumeric — same
    normalisation as pipeline._normalize_token, inlined to keep this module
    network- and import-free (pure + unit-testable). Lets "podía"/"podia" and
    "Edén,"/"eden" match when we re-anchor lyric lines to the word stream."""
    import unicodedata as _u
    s = _u.normalize("NFD", (s or "").lower())
    return "".join(c for c in s if c.isalnum() and not _u.combining(c))


def wordstamps_to_segments(
    wordstamps: list[dict], lyric_lines: list[str], *, max_tail_dur: float = 1.5,
    max_drift: int = 8, min_anchor_score: float = 0.5, drift_abort: int = 4,
) -> list[dict]:
    """Reconstruct per-line segments from the word-level timestamps + the
    original line structure. Pure + testable.

    RE-ANCHORING (incident "Cosas Mías" class): the model is given the lyric
    transcript and *should* return one word stamp per transcript word in
    order — but it tokenises differently than `str.split()` (contractions,
    numbers, hyphens, dropped/duplicated tokens), so a naive positional walk
    (`wordstamps[cur:cur+wc]`, `cur += wc`) accumulates DRIFT: once the count
    diverges, every later line grabs the wrong stamps and the whole song slips
    out of sync. Instead, for each lyric line we search a small window around
    the running cursor for the best token-set match and re-anchor the cursor to
    the matched span's end — so a local divergence self-corrects on the next
    line instead of compounding. When matching collapses for several
    consecutive lines (the alignment is unreliable) we return [] so the caller
    falls back to its other timing paths.

    De-stretch (incident: Hermanos de Sangre): the model STRETCHES the last
    word of a line to fill the instrumental gap up to the next sung line, so a
    3-s line ends up held 12 s. We detect a ballooned trailing word (far above
    the song's median word) and cap the line's `end` to where that word
    actually STARTED + a normal tail — leaving silence instead of a frozen
    subtitle. Sung lines keep their real timing.

    Enforces monotonic, non-overlapping segments (clamp end to next start).
    """
    n_words = len(wordstamps)
    durs = sorted(d for d in (_word_dur(w) for w in wordstamps) if d is not None)
    median = durs[len(durs) // 2] if durs else 0.3
    stretch_thresh = max(max_tail_dur, median * 4)   # trailing word "too long"
    normal_tail = max(median * 1.5, 0.4)             # how long a real tail holds

    norm_words = [_norm(w.get("word", "")) for w in wordstamps]

    segs: list[dict] = []
    cur = 0          # running cursor into the word stream
    prev_start = -1  # first word index of the previous matched span
    drift_streak = 0
    for raw in lyric_lines:
        line = (raw or "").strip()
        if not line:
            continue
        line_tokens = [t for t in (_norm(x) for x in line.split()) if t]
        wc = len(line_tokens)
        if wc == 0 or cur >= n_words:
            continue
        line_set = set(line_tokens)

        # Search windows of length `wc` whose start sits in
        # [cur - small backslack .. cur + max_drift], never re-using the
        # previous line's first word. Pick the best token-set Jaccard;
        # ties resolve to the earliest (closest to the cursor) start.
        lo = max(prev_start + 1, cur - 2, 0)
        hi = min(n_words - wc, cur + max_drift)
        best_start, best_score = -1, -1.0
        for st in range(lo, hi + 1):
            win_set = {t for t in norm_words[st:st + wc] if t}
            if not win_set:
                continue
            inter = len(line_set & win_set)
            union = len(line_set | win_set)
            score = inter / union if union else 0.0
            if score > best_score:
                best_score, best_start = score, st

        if best_start < 0 or best_score < min_anchor_score:
            # Couldn't anchor this line — keep position (best effort) and
            # count it toward the drift-abort budget.
            best_start = min(cur, max(0, n_words - wc))
            drift_streak += 1
        else:
            drift_streak = 0

        if drift_streak >= drift_abort:
            # Alignment has lost the plot for several lines running — bail so
            # the caller falls back rather than ship a drifting transcript.
            return []

        span = wordstamps[best_start:best_start + wc]
        if not span:
            continue
        try:
            start = float(span[0].get("start"))
            last_start = float(span[-1].get("start"))
            end = float(span[-1].get("end"))
        except (TypeError, ValueError):
            continue
        # Trailing word stretched across a gap → trim it back.
        if (end - last_start) > stretch_thresh:
            end = last_start + normal_tail
        if end < start:
            end = start
        # Preserve per-word stamps + score on the line so the editor can do
        # karaoke + confidence-highlighting. Each word carries its original
        # start/end and (if the model returned probabilities) score 0-1.
        words_out = []
        for w in span:
            try:
                w_obj = {
                    "word": str(w.get("word", "")).strip(),
                    "start": float(w.get("start")),
                    "end": float(w.get("end")),
                }
                if w.get("score") is not None:
                    w_obj["score"] = float(w.get("score"))
                if w_obj["word"]:
                    words_out.append(w_obj)
            except (TypeError, ValueError):
                continue
        seg_out = {"start": start, "end": end, "text": line}
        if words_out:
            seg_out["words"] = words_out
        segs.append(seg_out)

        prev_start = best_start
        cur = best_start + wc   # re-anchor the cursor past the matched span

    segs.sort(key=lambda s: s["start"])
    for i in range(len(segs) - 1):
        if segs[i]["end"] > segs[i + 1]["start"]:
            segs[i]["end"] = max(segs[i]["start"], segs[i + 1]["start"] - 0.05)
    return segs


def _compress_for_upload(audio_path: str) -> tuple[str, bool]:
    """Transcode to a small mono 128 kbps mp3 so the Replicate upload is a
    few MB. A raw 40-60 MB WAV intermittently fails the upload with
    `Broken pipe` (observed in prod), which made forced align silently fall
    back. Alignment accuracy is bounded well above 128 kbps mono, so this
    is lossless for our purpose. Returns (path, is_temp); falls back to the
    original on any ffmpeg error."""
    out = None
    try:
        fd, out = tempfile.mkstemp(suffix=".fa.mp3")
        os.close(fd)
        subprocess.run(
            ["ffmpeg", "-y", "-i", audio_path, "-ac", "1", "-b:a", "128k",
             "-loglevel", "error", out],
            check=True, timeout=180, capture_output=True, text=True,
        )
        if os.path.exists(out) and os.path.getsize(out) > 0:
            return out, True
    except Exception as e:
        logger.warning("[FORCED] audio compress failed (%s) — using original", e)
    if out and os.path.exists(out):
        try:
            os.unlink(out)
        except OSError:
            pass
    return audio_path, False


def forced_align_lyrics(audio_path: str, lyrics_text: str) -> list[dict] | None:
    """Align `lyrics_text` to `audio_path` via the hosted forced aligner.
    Returns per-line segments or None (disabled / failure / too thin).
    Never raises — callers fall back to their existing timing path.
    """
    if not is_enabled():
        return None
    lyric_lines = [ln.strip() for ln in (lyrics_text or "").splitlines() if ln.strip()]
    if len(lyric_lines) < 4:
        return None  # too short to be worth a forced-align call

    try:
        import replicate  # noqa: F401 — used inside `_call_with_budget`
    except ImportError:
        logger.warning("[FORCED] replicate SDK not installed — falling back")
        return None

    # Pre-flight: belt-and-suspenders validation of the audio before we pay
    # the upload + compress cost. `vocal_sep.separate_vocals` already
    # validates its stems, but this function is ALSO called with raw user
    # audio (no demucs in front) — a 50 ms clip uploaded by mistake would
    # otherwise crash cureau with the [1,2,1] padding bug. The probe is
    # cheap (ffprobe, ~30-100 ms) compared to the network round trip.
    try:
        from vocal_sep import validate_stem as _validate_stem
        ok, reason = _validate_stem(audio_path)
        if not ok:
            logger.warning("[FORCED] audio rejected pre-flight (%s) — falling back",
                           reason)
            return None
    except Exception as e:  # import error in tests / unexpected
        logger.debug("[FORCED] pre-flight validation skipped (%s)", e)

    transcript = "\n".join(lyric_lines)
    upload_path, is_temp = _compress_for_upload(audio_path)

    # POST-COMPRESS validation (HOTFIX 2026-05-24): the pre-flight
    # `_validate_stem` above runs on the SOURCE audio. `_compress_for_upload`
    # transcodes via ffmpeg → mono 128k MP3. If the source has a valid
    # header but the body is silent/corrupt (real case observed in
    # production: cureau crashed with `Expected 2D or 3D tensor ... got
    # [1, 2, 0]`), the post-compress MP3 may have 0 effective samples
    # even though the size > 0 byte gate passed.
    # Validate the upload_path the same way before paying the network
    # round-trip. If it fails, fall back instead of feeding zero samples
    # to the model.
    try:
        from vocal_sep import validate_stem as _validate_stem
        ok2, reason2 = _validate_stem(upload_path)
        if not ok2:
            logger.warning("[FORCED] post-compress audio invalid (%s) — falling back",
                           reason2)
            if is_temp:
                try: os.unlink(upload_path)
                except OSError: pass
            return None
    except Exception as e:
        logger.debug("[FORCED] post-compress validation skipped (%s)", e)

    # Build a fresh input dict per attempt — the audio file handle is
    # consumed on each upload, so we need to reopen it.
    def _input_factory():
        return {
            "audio_file": open(upload_path, "rb"),
            "transcript": transcript,
            # show_probabilities returns per-word `score` (0-1) which we
            # surface to the editor for confidence highlighting on
            # low-certainty lines.
            "show_probabilities": True,
        }

    # Total wall-clock budget for the whole retry sequence. Default 8 min
    # covers worst-case observed in healthy runs (~3 min) with margin;
    # `FORCED_ALIGN_BUDGET_S` env override lets us extend at runtime if
    # Replicate stays degraded longer than expected.
    try:
        total_budget_s = float(os.environ.get("FORCED_ALIGN_BUDGET_S", "480"))
    except ValueError:
        total_budget_s = 480.0

    try:
        output = _call_with_budget(
            _MODEL, _input_factory,
            total_budget_s=total_budget_s,
            backoff=[0, 8, 24],
        )
    finally:
        if is_temp:
            try:
                os.unlink(upload_path)
            except OSError:
                pass
    if output is None:
        return None

    words = (
        (output.get("wordstamps") or output.get("words"))
        if isinstance(output, dict) else output
    )
    if not words:
        logger.warning("[FORCED] empty wordstamps — falling back")
        return None

    segs = wordstamps_to_segments(words, lyric_lines)
    # Require at least half the lines (and >=4) to have aligned, else the
    # result is unreliable and we fall back.
    if len(segs) < max(4, int(0.5 * len(lyric_lines))):
        logger.warning(
            "[FORCED] thin alignment (%s/%s lines) — falling back",
            len(segs), len(lyric_lines),
        )
        return None
    logger.info(
        "[FORCED] aligned %s/%s lines via forced alignment",
        len(segs), len(lyric_lines),
    )
    return segs
