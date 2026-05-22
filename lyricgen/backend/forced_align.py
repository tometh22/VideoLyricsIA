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


def wordstamps_to_segments(
    wordstamps: list[dict], lyric_lines: list[str], *, max_tail_dur: float = 1.5,
) -> list[dict]:
    """Reconstruct per-line segments from the word-level timestamps + the
    original line structure: walk each line's word count through the word
    stream; line start = first word's start. Pure + testable.

    De-stretch (incident: Hermanos de Sangre): the forced-align model
    (stable-ts/whisperX) STRETCHES the last word of a line to fill the
    instrumental gap up to the next sung line, so a 3-s line ends up held
    on screen for 12 s. We detect a ballooned trailing word (duration far
    above the song's median word) and cap the line's `end` to where that
    word actually STARTED + a normal word's worth — leaving the gap as
    silence instead of a frozen subtitle. The sung lines keep their real
    timing.

    Enforces monotonic, non-overlapping segments (clamp end to next start).
    """
    durs = sorted(d for d in (_word_dur(w) for w in wordstamps) if d is not None)
    median = durs[len(durs) // 2] if durs else 0.3
    stretch_thresh = max(max_tail_dur, median * 4)   # trailing word "too long"
    normal_tail = max(median * 1.5, 0.4)             # how long a real tail holds

    segs: list[dict] = []
    cur = 0
    for raw in lyric_lines:
        line = (raw or "").strip()
        if not line:
            continue
        wc = len(line.split())
        span = wordstamps[cur:cur + wc]
        cur += wc
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
        segs.append({"start": start, "end": end, "text": line})

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
        import replicate
    except ImportError:
        logger.warning("[FORCED] replicate SDK not installed — falling back")
        return None

    transcript = "\n".join(lyric_lines)
    upload_path, is_temp = _compress_for_upload(audio_path)
    output = None
    last_err = None
    try:
        # Retry the upload/run once: large-file uploads to Replicate can
        # drop with Broken pipe intermittently.
        for attempt in range(2):
            try:
                with open(upload_path, "rb") as audio:
                    output = replicate.run(
                        _MODEL, input={"audio_file": audio, "transcript": transcript},
                    )
                break
            except Exception as e:  # network, billing, model error, anything
                last_err = e
                logger.warning("[FORCED] replicate attempt %s failed (%s)", attempt + 1, e)
    finally:
        if is_temp:
            try:
                os.unlink(upload_path)
            except OSError:
                pass
    if output is None:
        logger.warning("[FORCED] replicate failed after retries (%s) — falling back", last_err)
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
