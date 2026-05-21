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


def wordstamps_to_segments(
    wordstamps: list[dict], lyric_lines: list[str],
) -> list[dict]:
    """Reconstruct per-line segments from the word-level timestamps + the
    original line structure: walk each line's word count through the word
    stream; line start/end = first/last word's start/end. Pure + testable.

    Enforces monotonic, non-overlapping segments (clamp end to next start).
    """
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
            end = float(span[-1].get("end"))
        except (TypeError, ValueError):
            continue
        if end < start:
            end = start
        segs.append({"start": start, "end": end, "text": line})

    segs.sort(key=lambda s: s["start"])
    for i in range(len(segs) - 1):
        if segs[i]["end"] > segs[i + 1]["start"]:
            segs[i]["end"] = max(segs[i]["start"], segs[i + 1]["start"] - 0.05)
    return segs


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
    try:
        with open(audio_path, "rb") as audio:
            output = replicate.run(
                _MODEL, input={"audio_file": audio, "transcript": transcript},
            )
    except Exception as e:  # network, billing, model error, anything
        logger.warning("[FORCED] replicate call failed (%s) — falling back", e)
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
