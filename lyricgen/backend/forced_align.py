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
from typing import Any

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


def _safe_provider_value(value: Any, *, _seen: set[int] | None = None) -> Any:
    """Return a JSON-safe lossless-enough view of a Replicate completion.

    Normal Replicate outputs are already JSON values.  The recursive fallback
    exists for SDK wrappers or malformed rows: every list entry survives, and
    only an opaque object's bounded string representation replaces that object.
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else str(value)

    seen = _seen if _seen is not None else set()
    identity = id(value)
    if identity in seen:
        return {"raw": "<recursive-provider-value>"}
    seen.add(identity)
    try:
        if isinstance(value, dict):
            return {
                str(key): _safe_provider_value(item, _seen=seen)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [
                _safe_provider_value(item, _seen=seen) for item in value
            ]
        try:
            dumped = value.model_dump()
        except Exception:
            dumped = None
        if dumped is not None and dumped is not value:
            return _safe_provider_value(dumped, _seen=seen)
        return {"raw": str(value)[:2000]}
    finally:
        seen.discard(identity)


def _raw_replicate_events(output: Any) -> list[dict]:
    """Wrap one completed Replicate payload as one durable provider event."""
    return [{"provider_output": _safe_provider_value(output)}]


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
        from replicate_budget import (
            finish_replicate_provenance,
            start_replicate_provenance,
        )
        recorder = None
        try:
            inputs = input_factory()
            recorder = start_replicate_provenance(model, "FORCED", attempt + 1)
            result = replicate.run(model, input=inputs)
            # The shared helper serves canonical forced alignment and short
            # gap alignment. Freeze the complete provider payload here, before
            # either caller extracts wordstamps or rejects a thin/malformed
            # alignment. Failed network attempts have no completed output and
            # therefore are intentionally not counted.
            from recognition_provenance import record_completed
            record_completed(
                family=model,
                events=_raw_replicate_events(result),
                kind="forced_alignment",
                view="forced_alignment_audio",
                transformation="replicate_forced_align_raw",
            )
            finish_replicate_provenance(recorder, "succeeded")
            return result
        except Exception as e:
            finish_replicate_provenance(
                recorder,
                f"error: {type(e).__name__}: {str(e)[:300]}",
            )
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


# Gate texto-vs-audio del fallback best-effort (ANCHOR_TEXT_GATE_ENABLED,
# default off). Umbral bajo a propósito: no busca calidad, busca descartar
# solo lo INDEFENDIBLE — una línea cuyo texto ni siquiera suena a lo que se
# canta en la ventana donde caería.
_TEXT_GATE_MIN = 0.5


def _text_gate_enabled() -> bool:
    return os.environ.get(
        "ANCHOR_TEXT_GATE_ENABLED", "0"
    ).strip().lower() in ("1", "true", "yes", "on")


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

    NO TAIL PILE-UP (incident "638" 2026-05-26, again Los Pericos "Runaway (En
    Vivo)" 2026-08-05): when the reference has MORE content than the recording
    (studio lyric over a radio edit / live cut), the surplus lines have nowhere
    to anchor and the best-effort fallback below pins each of them to the same
    end-of-stream window — the operator sees a stack of lines sharing one
    timestamp ("17 lines at 1:50", "5 lines stuck at exactly 1:15.5"). Neither
    guard upstream catches it: `drift_abort` is reset by the lines that DO
    anchor, and the text gate passes because the final window genuinely sounds
    like the repeated chorus those lines contain. So we check the geometry
    instead: when a line fails to anchor AND lands pinned at the end of the word
    stream, the cursor has already consumed the recording — the reference is in
    song order, so nothing after it is sung either, and we stop. Nothing that
    anchored is affected.

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

        anchored = not (best_start < 0 or best_score < min_anchor_score)
        if not anchored:
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

        # Tail pile-up guard (see docstring). An un-anchored line pinned at the
        # very end of the word stream means the cursor has consumed the whole
        # recording but the reference still has lines: emitting them stacks them
        # all onto the final timestamp. The reference is in song order, so once
        # one line has no audio left neither does anything after it — stop here
        # instead of letting the remainder crowd into the last seconds.
        # `n_words > wc` keeps the degenerate "line longer than the whole
        # transcript" case out of it.
        if not anchored and n_words > wc and best_start >= n_words - wc:
            logger.info(
                "[FA] la referencia sigue pero el audio se terminó — se corta "
                "la cola en %r en vez de apilarla en el último timestamp",
                line[:48],
            )
            break

        if not anchored and _text_gate_enabled():
            # Gate texto-vs-audio (ANCHOR_TEXT_GATE_ENABLED, default off).
            # El fallback best-effort emite la línea EN LA POSICIÓN DEL
            # CURSOR aunque ninguna ventana haya matcheado — así una línea
            # de la estrofa 2 quedó pintada sobre el audio del estribillo
            # (job 6f4047db: líneas en 92s/104s con 0 % de coincidencia
            # contra lo cantado), dejando vacío su lugar real, que la
            # recuperación de huecos rellenó después → texto duplicado en
            # pantalla. Antes de emitir una línea no-anclada, medimos si su
            # texto al menos SUENA a las palabras de la ventana donde va a
            # caer; si ni eso, se descarta — la zona queda como hueco y la
            # recuperación de huecos la cubre con lo que realmente se canta.
            # El strike de drift ya quedó contado: el presupuesto de abort
            # no cambia.
            _win = [t for t in norm_words[best_start:best_start + wc] if t]
            if _win and _phonetic_ratio(line_tokens, _win) < _TEXT_GATE_MIN:
                logger.info(
                    "[FA] text-gate: línea %r no suena a la ventana donde "
                    "caería — descartada en vez de emitida mal ubicada",
                    line[:48],
                )
                continue

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
