"""WhisperX transcription — word-level timestamps for the NO-LYRICS path.

WHY
---
When we have no known lyrics (lrclib 404, Gemini empty) the audio is the only
source of truth. OpenAI's whisper-1 gives segment-level timing (±500ms) and
interpolates word times. WhisperX (Whisper large-v2 + wav2vec2 phoneme forced
alignment + VAD) pins each word to <100ms and its VAD makes it far less prone to
the single-mega-segment hallucination. This is the engine behind Rotor-grade
"transcribe a hard song with no lyrics and it still lands". Run on Replicate so
there's no torch/GPU on the workers (same plumbing as `forced_align.py`).

CONTRACT
--------
- Behind `WHISPERX_ENABLED` (default off) + `REPLICATE_API_TOKEN`.
- `transcribe_whisperx(audio_path, language=None) -> list[dict] | None` returns
  `[{"start","end","text","words":[{"word","start","end"}]}]` aligned to the
  audio, or **None** on any failure / when disabled / when the result looks
  empty. NEVER raises — the caller falls back to whisper-1.
- `_map_segments` is pure (no network) and unit-testable.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("genly.whisperx")

_TRUE = ("1", "true", "yes", "on", "y", "t")

# Replicate whisperX model. Override via env once a version is verified for
# prod. NOTE: pin to a known-good version hash before enabling in production.
_MODEL = os.environ.get(
    "WHISPERX_MODEL",
    # Verified live 2026-05-22 via Replicate API (account tometh22).
    "victor-upmeet/whisperx:655845d6190ef70573c669245f245892cd039df4b880a1e3a65852c09252f5cc",
)


def is_enabled() -> bool:
    """On only when the flag is set AND a Replicate token is present."""
    flag = os.environ.get("WHISPERX_ENABLED", "0").strip().lower() in _TRUE
    return flag and bool(os.environ.get("REPLICATE_API_TOKEN", "").strip())


def _map_segments(output) -> list[dict]:
    """Map whisperX output to our segment shape. Pure + testable.

    whisperX returns {"segments": [{"start","end","text",
    "words":[{"word","start","end","score"}]}], ...}. We keep line-level
    start/end/text and carry per-word stamps (for a future word-level editor).
    Drops segments with no usable text or non-numeric bounds.
    """
    if isinstance(output, dict):
        raw = output.get("segments") or []
    elif isinstance(output, list):
        raw = output
    else:
        return []

    segs: list[dict] = []
    for s in raw:
        if not isinstance(s, dict):
            continue
        text = (s.get("text") or "").strip()
        if not text:
            continue
        try:
            start = float(s.get("start"))
            end = float(s.get("end"))
        except (TypeError, ValueError):
            continue
        if end < start:
            end = start
        words = []
        for w in (s.get("words") or []):
            if not isinstance(w, dict):
                continue
            wt = (w.get("word") or w.get("text") or "").strip()
            try:
                ws = float(w.get("start"))
                we = float(w.get("end"))
            except (TypeError, ValueError):
                continue  # whisperX omits stamps for non-alignable tokens
            if wt:
                words.append({"word": wt, "start": ws, "end": we})
        seg = {"start": start, "end": end, "text": text}
        if words:
            seg["words"] = words
        segs.append(seg)
    return segs


def _filter_ghosts(segs: list[dict]) -> list[dict]:
    """Drop suspicious tiny segments: whisperX occasionally tags an
    instrumental flourish or breath as a 1-word segment (real example on
    El Arbol intro: `'Amén'` at 5.15s for 0.18s). Anything <0.5s AND
    <2 words is treated as a ghost. Single-word holds longer than 0.5s
    (e.g., a chanted name) are kept. Pure + testable."""
    out: list[dict] = []
    for s in segs:
        try:
            dur = float(s.get("end", 0)) - float(s.get("start", 0))
        except (TypeError, ValueError):
            dur = 0.0
        words = len((s.get("text") or "").split())
        if dur < 0.5 and words < 2:
            continue
        out.append(s)
    return out


def _split_long_segments(segs: list[dict], *, max_dur: float = 12.0,
                          min_split_gap: float = 0.3) -> list[dict]:
    """Split segments longer than `max_dur` at the largest internal
    word-to-word gap, recursively, so subtitles aren't 15+ second walls of
    text. Requires per-word stamps (whisperX provides them with
    `align_output=True`); segments without words are left untouched.

    The split point is the BIGGEST gap >= `min_split_gap` (a real pause).
    No usable gap → no split. Recursion is bounded by re-checking each
    half's duration. Pure + testable.
    """
    if max_dur <= 0:
        return segs

    def _split_once(seg: dict) -> list[dict]:
        words = seg.get("words") or []
        if len(words) < 4:
            return [seg]
        try:
            start = float(seg["start"]); end = float(seg["end"])
        except (TypeError, ValueError, KeyError):
            return [seg]
        if (end - start) <= max_dur:
            return [seg]
        # Find biggest gap between consecutive words.
        best_i, best_gap = -1, 0.0
        for i in range(len(words) - 1):
            try:
                gap = float(words[i + 1]["start"]) - float(words[i]["end"])
            except (TypeError, ValueError, KeyError):
                continue
            if gap > best_gap:
                best_gap, best_i = gap, i
        if best_i < 0 or best_gap < min_split_gap:
            return [seg]   # continuous singing, can't find a natural break
        # Split AT the gap: left = words[:best_i+1], right = words[best_i+1:]
        left_words = words[: best_i + 1]
        right_words = words[best_i + 1:]
        left = {
            "start": float(left_words[0]["start"]),
            "end": float(left_words[-1]["end"]),
            "text": " ".join(w.get("word", "").strip() for w in left_words if w.get("word")),
            "words": left_words,
        }
        right = {
            "start": float(right_words[0]["start"]),
            "end": float(right_words[-1]["end"]),
            "text": " ".join(w.get("word", "").strip() for w in right_words if w.get("word")),
            "words": right_words,
        }
        # Recurse on each half (depth-bounded by shrinking duration).
        return _split_once(left) + _split_once(right)

    out: list[dict] = []
    for s in segs:
        out.extend(_split_once(s))
    return out


def transcribe_whisperx(audio_path: str, language: str | None = None) -> list[dict] | None:
    """Transcribe `audio_path` with whisperX. Returns segments with word
    stamps, or None (disabled / failure / empty). Never raises.
    """
    if not is_enabled():
        return None
    if not audio_path or not os.path.exists(audio_path):
        return None

    try:
        import replicate
    except ImportError:
        logger.warning("[WHISPERX] replicate SDK not installed — falling back")
        return None

    payload: dict = {"align_output": True}
    if language:
        payload["language"] = language
    try:
        with open(audio_path, "rb") as audio:
            output = replicate.run(_MODEL, input={"audio_file": audio, **payload})
    except Exception as e:  # network, billing, model error, anything
        logger.warning("[WHISPERX] replicate call failed (%s) — falling back", e)
        return None

    segs = _map_segments(output)
    raw_n = len(segs)
    segs = _filter_ghosts(segs)
    segs = _split_long_segments(segs)
    if len(segs) < 2:
        logger.warning("[WHISPERX] thin/empty result (%s raw -> %s usable) — falling back",
                       raw_n, len(segs))
        return None
    logger.info("[WHISPERX] transcribed %s segments (%s raw, after ghost-filter + split)", len(segs), raw_n)
    return segs
