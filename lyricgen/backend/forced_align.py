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
#
# 2026-05-26 (LIVE PROD INCIDENT, Sin Gamulán):
# `Expected 2D or 3D (batch mode) tensor with possibly 0 batch size and
# other non-zero dimensions for input, but got: [1, 2, 0]`
# es otra falla determinista de cureau sobre vocal stems con cierta
# combinación de letras altamente repetitivas. Sin el fragment match,
# cureau reintentaba 3× × ~30s = 90+ s de espera antes de que el caller
# (main.py:_run_transcription_for_job, audio-as-truth path) cayera al
# synced-direct fallback (PR #365). Operador veía "0% transcribiendo
# 120 s" en el editor. Con el fragment, FA aborta en attempt 1 (<5 s)
# y synced-direct se dispara inmediato.
_NON_RETRYABLE_FRAGMENTS = (
    "padding size",
    "expected 2d or 3d",   # cureau [1, 2, 0] tensor crash — Sin Gamulán family
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


def _phonetic_ratio(line_tokens: list[str], window_tokens: list[str]) -> float:
    """SequenceMatcher ratio over the alphanumeric-only concatenation of both
    sides. Catches acoustic mishears that Jaccard misses because the surface
    tokens diverge (whisperX heard 'Le realizan la', canonical is 'Legalícenla'
    — Jaccard=0, but the compact strings 'lerealizanla' vs 'legalicenla' share
    enough characters in order to score ~0.70).

    Pure + deterministic — uses stdlib `difflib.SequenceMatcher`, no extra
    dependency. Returns 0.0 when either side is empty."""
    from difflib import SequenceMatcher
    a = "".join(line_tokens)
    b = "".join(window_tokens)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


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

    PHONETIC SCORING (incident Legalícenla intro mishear, 2026-05-25): when
    whisperX is fed audio without lyrics_hint coverage of every section, it
    mis-transcribes acoustically-similar regions (the intro chorus "Legalícenla"
    came back as "Le realizan la" × 3). Plain Jaccard over surface tokens scored
    0.0 against the canonical line and reconcile aborted by drift, dumping the
    user at "first lyric @ 0:45" when the real first sung word is at 0:17.
    We now score `max(jaccard_tokens, phonetic_ratio)` where the phonetic side
    is SequenceMatcher over the alphanumeric-compact strings. "lerealizanla"
    vs "legalicenla" → 0.696, well above min_anchor_score=0.5, so the line
    anchors to its true acoustic location and the canonical text wins. The
    Jaccard path stays primary for clean alignments — phonetic only rescues
    cases where surface tokens diverge.

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
        # previous line's first word. Pick the best score among (a) Jaccard
        # over token sets and (b) SequenceMatcher ratio over compact
        # alphanumeric strings. Phonetic wins on acoustic mishears that share
        # characters in order (Legalícenla / le realizan la) where Jaccard
        # collapses to 0. Ties resolve to the earliest start.
        lo = max(prev_start + 1, cur - 2, 0)
        hi = min(n_words - wc, cur + max_drift)
        best_start, best_score = -1, -1.0
        for st in range(lo, hi + 1):
            win_tokens = [t for t in norm_words[st:st + wc] if t]
            if not win_tokens:
                continue
            win_set = set(win_tokens)
            inter = len(line_set & win_set)
            union = len(line_set | win_set)
            jaccard = inter / union if union else 0.0
            phonetic = _phonetic_ratio(line_tokens, win_tokens)
            score = max(jaccard, phonetic)
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


def _cureau_wordstamps(
    audio_path: str, transcript: str, *, total_budget_s: float = 180.0,
    validate: bool = True,
) -> list[dict] | None:
    """Low-level: call cureau on ONE clip and return its raw wordstamps
    (`[{"word","start","end"[,"score"]}]`) in clip-local time, or None on
    any failure. Does NOT reconstruct per-line segments — that's the
    caller's job (chunked stitcher runs `wordstamps_to_segments` once over
    the stitched stream). Never raises.

    Factored out of `forced_align_lyrics` so the chunked aligner can reuse
    the exact same upload/compress/validation/budget machinery on a short
    clip — a short clip is what AVOIDS the cureau [1,2,0]/[1,2,1] tensor
    crash on highly-repetitive full songs (the live "Nada Fue" stem). The
    full-song `forced_align_lyrics` is unchanged.
    """
    try:
        import replicate  # noqa: F401
    except ImportError:
        return None
    if validate:
        try:
            from vocal_sep import validate_stem as _validate_stem
            ok, reason = _validate_stem(audio_path)
            if not ok:
                logger.warning("[FORCED] clip rejected pre-flight (%s)", reason)
                return None
        except Exception as e:
            logger.debug("[FORCED] clip pre-flight skipped (%s)", e)

    upload_path, is_temp = _compress_for_upload(audio_path)
    _open_handles: list = []

    def _input_factory():
        f = open(upload_path, "rb")
        _open_handles.append(f)
        return {
            "audio_file": f,
            "transcript": transcript,
            "show_probabilities": True,
        }

    try:
        output = _call_with_budget(
            _MODEL, _input_factory,
            total_budget_s=total_budget_s,
            backoff=[0, 8],
        )
    finally:
        for _h in _open_handles:
            try:
                _h.close()
            except Exception:
                pass
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
        return None
    # Coerce to clean float-typed dicts.
    out: list[dict] = []
    for w in words:
        try:
            obj = {
                "word": str(w.get("word", "")).strip(),
                "start": float(w.get("start")),
                "end": float(w.get("end")),
            }
        except (TypeError, ValueError, AttributeError):
            continue
        if w.get("score") is not None:
            try:
                obj["score"] = float(w.get("score"))
            except (TypeError, ValueError):
                pass
        if obj["word"]:
            out.append(obj)
    return out or None


def _slice_clip(audio_path: str, start_s: float, dur_s: float) -> str | None:
    """ffmpeg-slice [start, start+dur] into a temp mp3 (sample-accurate
    seek + re-encode, like pipeline._slice_audio_window). Returns the temp
    path (caller unlinks) or None on failure. Never raises."""
    if start_s < 0 or dur_s <= 0:
        return None
    out = None
    try:
        fd, out = tempfile.mkstemp(suffix=".fa_clip.mp3")
        os.close(fd)
        subprocess.run(
            ["ffmpeg", "-y", "-i", audio_path,
             "-ss", str(start_s), "-t", str(dur_s),
             "-ac", "1", "-b:a", "128k", "-loglevel", "error", out],
            check=True, timeout=120, capture_output=True, text=True,
        )
        if os.path.exists(out) and os.path.getsize(out) > 0:
            return out
    except Exception as e:
        logger.warning("[FORCED] clip slice failed @%.1fs (%s)", start_s, e)
    if out and os.path.exists(out):
        try:
            os.unlink(out)
        except OSError:
            pass
    return None


def _audio_duration_s(audio_path: str) -> float | None:
    """ffprobe duration in seconds, or None. Never raises."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", audio_path],
            capture_output=True, text=True, timeout=30, check=True,
        )
        return float(r.stdout.strip())
    except Exception as e:
        logger.debug("[FORCED] ffprobe duration failed (%s)", e)
        return None


def _plan_windows(
    duration_s: float, vocal_regions, *,
    target_s: float = 30.0, overlap_s: float = 3.0, max_s: float = 40.0,
) -> list[tuple[float, float]]:
    """Plan [(start, end)] alignment windows over the audio.

    If `vocal_regions` is a *useful* VAD signal (≥2 regions that don't cover
    the whole song — i.e. a real isolated stem with silent instrumental
    gaps), GROUP consecutive voiced regions into ~`target_s` windows, breaking
    at the widest available gap once a group reaches the target. Window edges
    then land in SILENCE, so no sung word is split across a seam (the cleanest
    possible chunking) and each window's time span tightly brackets the lines
    that actually sing in it (a far better line-assignment prior than the
    uniform proportional guess). A single voiced region longer than `max_s` is
    sub-tiled. Otherwise (full mix → VAD returns one giant region, or no
    signal) fall back to uniform `target_s` windows over the whole duration.
    All windows pad by `overlap_s`. Pure + testable.
    """
    windows: list[tuple[float, float]] = []
    useful = False
    if vocal_regions and len(vocal_regions) >= 2:
        covered = sum(max(0.0, b - a) for a, b in vocal_regions)
        # "Useful" only if the voiced span is meaningfully less than the
        # whole song (real gaps to chunk on). A full mix's single 0..dur
        # region fails len>=2; a stem with tiny gaps that still covers ~all
        # of the song fails this fraction gate → we tile uniformly instead.
        useful = covered < 0.92 * duration_s

    def _subtile(lo: float, hi: float):
        span = hi - lo
        if span <= 0:
            return
        if span <= max_s:
            windows.append((max(0.0, lo - overlap_s),
                            min(duration_s, hi + overlap_s)))
            return
        n = max(1, round(span / target_s))
        step = span / n
        for k in range(n):
            ws = lo + k * step
            we = lo + (k + 1) * step
            windows.append((max(0.0, ws - overlap_s),
                            min(duration_s, we + overlap_s)))

    if useful:
        regs = sorted(vocal_regions)
        grp_start = regs[0][0]
        grp_end = regs[0][1]
        for (a, b) in regs[1:]:
            # Extend the current group; close it once it reaches the target
            # (the break lands in the silent gap before `a`).
            if (b - grp_start) > target_s and (grp_end - grp_start) >= 1.0:
                if (grp_end - grp_start) > max_s:
                    _subtile(grp_start, grp_end)
                else:
                    windows.append((max(0.0, grp_start - overlap_s),
                                    min(duration_s, grp_end + overlap_s)))
                grp_start = a
            grp_end = b
        if grp_end - grp_start > max_s:
            _subtile(grp_start, grp_end)
        else:
            windows.append((max(0.0, grp_start - overlap_s),
                            min(duration_s, grp_end + overlap_s)))
    else:
        _subtile(0.0, duration_s)

    # Merge accidental zero/negative windows; clamp; sort.
    windows = [(round(s, 3), round(e, 3)) for s, e in windows if e - s > 1.0]
    windows.sort()
    return windows


def _line_expected_time(i: int, n: int, duration_s: float, vocal_regions):
    """Expected real-audio onset of line `i` of `n`, used to assign lines to
    windows. With a useful VAD signal we spread lines over CUMULATIVE VOICED
    time (lines sing during voiced spans, so the dense first half and the
    sparse outro are handled correctly — a uniform i/n·duration guess piles
    too many late lines into the final window). Without VAD, falls back to
    uniform. Pure."""
    if vocal_regions:
        voiced = sum(max(0.0, b - a) for a, b in vocal_regions)
        if voiced > 0:
            target = (i + 0.5) / n * voiced
            s = 0.0
            for a, b in sorted(vocal_regions):
                seg = b - a
                if s + seg >= target:
                    return a + (target - s)
                s += seg
            return sorted(vocal_regions)[-1][1]
    return (i + 0.5) / n * duration_s


def _assign_lines_to_windows(
    lyric_lines: list[str], windows: list[tuple[float, float]],
    duration_s: float, *, vocal_regions=None, context: int = 1,
) -> list[list[int]]:
    """Map lyric-line indices to alignment windows.

    Each line's expected onset (`_line_expected_time`, voiced-proportional when
    a VAD signal is available) is matched to the window(s) whose padded span
    contains it; if none, the nearest window by centre. A line is then extended
    by `context` neighbours into each adjacent window so a line straddling a
    boundary is offered to both — the downstream aligned-onset-in-core adoption
    then keeps only the copy that lands in the right window's core, and a too-
    greedy assignment can't pile distant lines into one window (the failure of
    the old uniform i/n guess). Returns one sorted index list per window.
    Pure + testable."""
    n = len(lyric_lines)
    if n == 0 or not windows:
        return [[] for _ in windows]
    sets: list[set] = [set() for _ in windows]
    for i in range(n):
        et = _line_expected_time(i, n, duration_s, vocal_regions)
        hit = [wi for wi, (ws, we) in enumerate(windows) if ws <= et < we]
        if not hit:
            hit = [min(range(len(windows)),
                       key=lambda k: abs(et - (windows[k][0] + windows[k][1]) / 2))]
        for wi in hit:
            sets[wi].add(i)
    # widen each window's block by `context` lines on each side
    out: list[list[int]] = []
    for s in sets:
        if not s:
            out.append([])
            continue
        lo_i = max(0, min(s) - context)
        hi_i = min(n - 1, max(s) + context)
        out.append(list(range(lo_i, hi_i + 1)))
    return out


def snap_starts_to_vocal_onsets(
    segs: list[dict], vocal_regions, *, max_snap_s: float = 2.0,
) -> list[dict]:
    """Snap each line's `start` to the nearest VAD vocal-region ONSET (the
    start of a voiced span on the isolated stem) within `max_snap_s`.

    WHY (jitter analysis, "Donde Están" stem 2026-06): a forced aligner places
    a line's first-word onset with ~0.5-1.6 s of slack, and the slack is ~3×
    worse on lines that follow an instrumental/section gap (measured: median
    |offset| 1.61 s after a ≥1.5 s gap vs 0.59 s mid-verse) — cureau either
    latches onto a pre-vocal transient early or misses the soft vowel attack
    and lands late. Meanwhile Rotor's ground-truth line starts sit right on the
    true vocal onset (measured: median |GT − stem-onset| = 0.07 s). So pinning
    each start to the stem's vocal-region onset removes the aligner's
    boundary jitter. Measured win on the chunked-stem result: "Donde Están" AOO
    median 0.99 → 0.56 s, de-offset 0.82 → 0.48 s; live "Nada Fue" de-offset
    0.48 → ~0.40 s. The `max_snap_s` cap keeps it from yanking a start onto a
    far-away wrong onset; per-word stamps are left untouched (truthful to the
    audio — only the line's display onset moves). Pure + testable; no-op when
    `vocal_regions` is empty."""
    if not vocal_regions or not segs:
        return segs
    onsets = sorted(float(a) for a, _b in vocal_regions)
    if not onsets:
        return segs
    import bisect
    out: list[dict] = []
    for s in segs:
        try:
            st = float(s.get("start"))
        except (TypeError, ValueError):
            out.append(s)
            continue
        # nearest onset to st
        j = bisect.bisect_left(onsets, st)
        best = None
        for k in (j - 1, j):
            if 0 <= k < len(onsets):
                if best is None or abs(onsets[k] - st) < abs(best - st):
                    best = onsets[k]
        if best is not None and abs(best - st) <= max_snap_s:
            ns = dict(s)
            ns["start"] = round(best, 3)
            if ns.get("end") is not None and float(ns["end"]) < ns["start"]:
                ns["end"] = round(ns["start"] + 0.5, 3)
            out.append(ns)
        else:
            out.append(s)
    # keep monotonic, non-overlapping after the snap
    out.sort(key=lambda s: float(s["start"]))
    for i in range(len(out) - 1):
        if float(out[i].get("end", out[i]["start"])) > float(out[i + 1]["start"]):
            out[i]["end"] = round(max(float(out[i]["start"]),
                                      float(out[i + 1]["start"]) - 0.05), 3)
    return out


def _window_cores(windows: list[tuple[float, float]], duration_s: float):
    """Core (non-overlapping) time interval owned by each window: the span
    each window covers BEST, bounded by the midpoint of its overlap with each
    neighbour. A line whose ALIGNED onset falls in a window's core is trusted
    from that window; an onset outside every core (e.g. a dead neighbour made
    this window stretch its tail into a region it doesn't really cover) is
    rejected so a failed window can't pollute its neighbours. Pure."""
    n = len(windows)
    cores = []
    for i, (ws, we) in enumerate(windows):
        lo = 0.0 if i == 0 else (windows[i - 1][1] + ws) / 2.0
        hi = duration_s if i == n - 1 else (we + windows[i + 1][0]) / 2.0
        cores.append((lo, hi))
    return cores


def _interpolate_missing(final: list, lyric_lines: list[str], duration_s: float):
    """Fill `None` slots in a per-line segment list by LINEAR interpolation
    between the nearest aligned neighbours (a dead/failed window's lines).
    Marks filled lines `interp=True` so the caller/editor can flag them for
    review. Lines before the first / after the last anchor extrapolate by a
    small constant. Pure."""
    n = len(final)
    known = [i for i in range(n) if final[i] is not None]
    if not known:
        return final
    per_line = duration_s / max(1, n)
    for i in range(n):
        if final[i] is not None:
            continue
        prev = [k for k in known if k < i]
        nxt = [k for k in known if k > i]
        if prev and nxt:
            p, q = prev[-1], nxt[0]
            sp, sq = final[p]["start"], final[q]["start"]
            t = sp + (sq - sp) * (i - p) / (q - p)
        elif prev:
            t = final[prev[-1]]["start"] + per_line
        elif nxt:
            t = max(0.0, final[nxt[0]]["start"] - per_line)
        else:
            t = (i + 0.5) * per_line
        final[i] = {"start": round(t, 2), "end": round(t + 2.0, 2),
                    "text": lyric_lines[i], "interp": True, "review": True}
    return final


def _assemble_windows(
    windows, line_map, per_window_words, lyric_lines, duration_s,
) -> tuple[list, int]:
    """Pure assembler: given each window's RAW wordstamps (clip-local time;
    None = that window failed), reconstruct per-line segments, adopt by
    aligned-onset-in-core, interpolate gaps. Returns (final_segments, covered).
    Shared by `forced_align_lyrics_chunked` and the offline debug harness so
    there is ONE assembly implementation. Pure + testable."""
    cores = _window_cores(windows, duration_s)
    n_lines = len(lyric_lines)
    candidates: dict[int, list] = {i: [] for i in range(n_lines)}
    for wi, (idxs, words) in enumerate(zip(line_map, per_window_words)):
        if not idxs or not words:
            continue
        ws = windows[wi][0]
        sub_lines = [lyric_lines[i] for i in idxs]
        # Reconstruct THIS window's lines from THIS window's words, in
        # clip-local time. Relax drift_abort: a single short clip's stream is
        # clean, and an aggressive abort would discard a whole otherwise-good
        # window over one repetitive chorus line.
        sub_segs = wordstamps_to_segments(words, sub_lines, drift_abort=99)
        lo, hi = cores[wi]
        centre = (lo + hi) / 2.0
        used = [False] * len(sub_lines)
        for seg in sub_segs:
            stext = (seg.get("text") or "").strip()
            for k, (orig_i, sl) in enumerate(zip(idxs, sub_lines)):
                if used[k] or sl.strip() != stext:
                    continue
                used[k] = True
                gstart = float(seg["start"]) + ws
                # Adopt only if the ALIGNED onset lands in this window's core
                # (±1 s slack). Onsets outside every core (a tail stretched
                # over a dead neighbour's gap) are rejected → interpolated.
                if lo - 1.0 <= gstart < hi + 1.0:
                    gseg = dict(seg)
                    gseg["start"] = round(gstart, 3)
                    gseg["end"] = round(float(seg["end"]) + ws, 3)
                    if "words" in gseg:
                        gseg["words"] = [
                            {**w, "start": round(float(w["start"]) + ws, 3),
                             "end": round(float(w["end"]) + ws, 3)}
                            for w in gseg["words"]
                        ]
                    candidates[orig_i].append((abs(gstart - centre), gseg))
                break

    final: list = [None] * n_lines
    covered = 0
    for i in range(n_lines):
        if candidates[i]:
            candidates[i].sort(key=lambda t: t[0])
            final[i] = candidates[i][0][1]
            covered += 1
    if covered == 0:
        return final, 0
    final = _interpolate_missing(final, lyric_lines, duration_s)
    final.sort(key=lambda s: s["start"])
    for i in range(len(final) - 1):
        if final[i]["end"] > final[i + 1]["start"]:
            final[i]["end"] = max(final[i]["start"], final[i + 1]["start"] - 0.05)
    return final, covered


def _align_one_window(
    audio_path: str, ws: float, we: float, sub_lines: list[str],
    *, budget_s: float, retries: int = 1, split_depth: int = 1,
) -> list[dict] | None:
    """Align ONE window's clip, with crash recovery. Returns CLIP-LOCAL
    wordstamps (start/end relative to `ws`) or None. Never raises.

    cureau's `[1,2,0]` tensor crash is INTERMITTENT per clip: the same clip +
    transcript frequently succeeds on a fresh call (observed live on the
    "Donde Están" intro — first call crashed, a later identical call returned
    30 words). `_cureau_wordstamps` aborts the crash immediately (it's
    non-retryable inside its own budget loop), so recovery happens HERE:
      (1) up to `retries`+1 fresh full-clip attempts;
      (2) if still failing, SPLIT the window in half (small overlap) and align
          each half against its proportional share of the lines — a shorter
          clip + shorter transcript reliably dodges the crash. Half-B words are
          shifted into the parent clip's local frame before concatenation.
    A window that exhausts all of this returns None; the caller interpolates
    its lines instead of crashing the song."""
    transcript = "\n".join(sub_lines)
    for attempt in range(retries + 1):
        clip = _slice_clip(audio_path, ws, we - ws)
        if not clip:
            return None
        try:
            words = _cureau_wordstamps(clip, transcript, total_budget_s=budget_s)
        finally:
            try:
                os.unlink(clip)
            except OSError:
                pass
        if words:
            return words
        if attempt < retries:
            logger.info("[FORCED] window %.1f-%.1fs failed — retry %s/%s",
                        ws, we, attempt + 1, retries)

    if split_depth <= 0 or len(sub_lines) < 2 or (we - ws) < 8.0:
        return None
    logger.info("[FORCED] window %.1f-%.1fs still failing — splitting in half", ws, we)
    pad = 1.5
    mid_t = (ws + we) / 2.0
    mid_l = max(1, len(sub_lines) // 2)
    a = _align_one_window(audio_path, ws, mid_t + pad, sub_lines[:mid_l],
                          budget_s=budget_s, retries=0, split_depth=split_depth - 1)
    b = _align_one_window(audio_path, max(ws, mid_t - pad), we, sub_lines[mid_l:],
                          budget_s=budget_s, retries=0, split_depth=split_depth - 1)
    if not a and not b:
        return None
    out: list[dict] = list(a or [])
    if b:
        # half-B clip started at (mid_t - pad); shift its local stamps into the
        # parent clip frame (which starts at ws).
        shift = max(ws, mid_t - pad) - ws
        for w in b:
            out.append({**w, "start": w["start"] + shift, "end": w["end"] + shift})
    out.sort(key=lambda w: w["start"])
    return out or None


def forced_align_lyrics_chunked(
    audio_path: str, lyrics_text: str, *, vocal_regions=None,
    target_s: float = 30.0, overlap_s: float = 3.0,
) -> list[dict] | None:
    """Chunked forced alignment — the crash-proof, repetitive-song variant.

    WHY (incident: live "Nada Fue Un Error", 367 s, heavily repetitive):
    cureau on the FULL stem crashes deterministically with the
    `Expected 2D or 3D ... [1, 2, 0]` tensor bug (and is slow, ~75-250 s).
    The crash is triggered by long, highly self-similar transcripts. The fix
    is to never hand cureau the whole song: we tile the audio into short
    (~30 s) windows with ~3 s overlap, give each window only the lyric lines
    that fall in its time span (proportional guess + a few lines of context),
    and align each SHORT clip (a short clip does NOT crash).

    Each window is reconstructed into per-line segments INDEPENDENTLY
    (`wordstamps_to_segments` on its own sub-lines + its own words, in
    clip-local time) and offset to global time. A line is adopted from a
    window only when its ALIGNED onset lands inside that window's CORE (the
    non-overlapping span the window covers best); when several windows vouch
    for a line we keep the one whose centre is closest. This stops a window
    that FAILS (crash, timeout, empty) from tanking the song — its lines are
    covered by no core, so we INTERPOLATE them from the aligned neighbours
    (flagged `interp`/`review`) instead of letting a surviving neighbour
    mis-stretch over the gap. Per-window reconstruction also dodges the
    seam-corruption a single stitched word stream suffers on repetitive
    choruses (duplicate tokens at overlaps tripping the positional re-anchor's
    drift-abort).

    `vocal_regions`: optional precomputed VAD windows (anchor_align.
    vocal_regions on an isolated stem). When they expose real instrumental
    gaps we tile inside the voiced spans; otherwise (full mix / no signal)
    we tile uniformly. Returns per-line segments, or None on total failure /
    disabled / too-thin. Never raises.
    """
    if not is_enabled():
        return None
    lyric_lines = [ln.strip() for ln in (lyrics_text or "").splitlines() if ln.strip()]
    if len(lyric_lines) < 4:
        return None
    if not audio_path or not os.path.exists(audio_path):
        return None
    try:
        import replicate  # noqa: F401
    except ImportError:
        logger.warning("[FORCED] replicate SDK not installed — falling back")
        return None

    duration_s = _audio_duration_s(audio_path)
    if not duration_s or duration_s <= 0:
        logger.warning("[FORCED] no audio duration — falling back to full align")
        return forced_align_lyrics(audio_path, lyrics_text)

    windows = _plan_windows(
        duration_s, vocal_regions, target_s=target_s, overlap_s=overlap_s,
    )
    if not windows:
        return forced_align_lyrics(audio_path, lyrics_text)
    line_map = _assign_lines_to_windows(
        lyric_lines, windows, duration_s, vocal_regions=vocal_regions,
    )

    try:
        per_clip_budget = float(os.environ.get("FORCED_ALIGN_CHUNK_BUDGET_S", "120"))
    except ValueError:
        per_clip_budget = 120.0

    n_lines = len(lyric_lines)
    per_window_words: list = []
    ok_windows = 0
    for (ws, we), idxs in zip(windows, line_map):
        if not idxs:
            per_window_words.append(None)
            continue
        sub_lines = [lyric_lines[i] for i in idxs]
        words = _align_one_window(
            audio_path, ws, we, sub_lines, budget_s=per_clip_budget,
        )
        if not words:
            logger.warning("[FORCED] window %.1f-%.1fs unrecoverable — interpolating its lines", ws, we)
            per_window_words.append(None)
            continue
        ok_windows += 1
        per_window_words.append(words)

    if ok_windows == 0:
        logger.warning("[FORCED] chunked: every window failed — falling back to full align")
        return forced_align_lyrics(audio_path, lyrics_text)

    final, covered = _assemble_windows(
        windows, line_map, per_window_words, lyric_lines, duration_s,
    )
    if covered < max(4, int(0.5 * n_lines)):
        logger.warning(
            "[FORCED] chunked thin coverage (%s/%s lines from %s/%s windows)",
            covered, n_lines, ok_windows, len(windows),
        )
        return None
    # Jitter fix: pin each line start to the true vocal onset (VAD on the
    # stem), capping the move. Gated ON by default when we have a VAD signal;
    # FORCED_ALIGN_ONSET_SNAP=0 disables, FORCED_ALIGN_SNAP_MAX_S tunes the cap.
    snap_on = os.environ.get("FORCED_ALIGN_ONSET_SNAP", "1").strip().lower() in _TRUE
    if snap_on and vocal_regions:
        try:
            cap = float(os.environ.get("FORCED_ALIGN_SNAP_MAX_S", "2.0"))
        except ValueError:
            cap = 2.0
        final = snap_starts_to_vocal_onsets(final, vocal_regions, max_snap_s=cap)
    logger.info(
        "[FORCED] chunked aligned %s/%s lines (%s interpolated) from %s/%s windows",
        covered, n_lines, n_lines - covered, ok_windows, len(windows),
    )
    return final


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
    # Cada llamada al factory abre un handle nuevo; los acumulamos para
    # poder cerrarlos en el `finally` aunque Replicate falle mid-upload
    # (timeout, cancel, exception). Sin esto el worker leakea un FD por
    # intento × 7 réplicas × 3 retries → eventual `OSError: Too many
    # open files`. Doble-close es no-op, así que es seguro listarlos
    # incluso si el SDK ya los cerró por su cuenta.
    _open_handles: list = []
    def _input_factory():
        f = open(upload_path, "rb")
        _open_handles.append(f)
        return {
            "audio_file": f,
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
        for _h in _open_handles:
            try:
                _h.close()
            except Exception:
                pass
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
