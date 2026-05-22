"""Vocal source separation (demucs) — isolate the vocal stem before
transcription / forced alignment.

WHY
---
Whisper (and forced aligners) transcribe a vocal-only track far better than a
full mix: the band masks consonants and tricks the model into hallucinating or
giving up (incident "El Arbol": a 346 s song returned as one phrase). Isolating
the voice with Meta's demucs measurably lifts lyric-transcription accuracy
(WER 47.2%→27.7%, arXiv 2506.15514) — this is a big part of why Rotor nails
hard songs even without provided lyrics. We run demucs on Replicate so there's
no torch/GPU on the (small, CPU) workers, reusing the same plumbing as
`forced_align.py`.

CONTRACT
--------
- Behind `VOCAL_SEP_ENABLED` (default off) + `REPLICATE_API_TOKEN`.
- `separate_vocals(audio_path) -> str | None` returns a path to the isolated
  vocal stem (a temp .wav/.mp3) or **None** on any failure / when disabled.
  It NEVER raises — the caller falls back to the original mixed audio.
- Caller is responsible for using the returned stem for transcription/alignment
  and for deleting it when done (it lives in the system temp dir).

CAVEAT (long-form): separated vocals can increase Whisper hallucinations on
long files. Mitigation lives in the CALLER: feed the stem to forced alignment /
whisperX (both VAD-gated), gate any bare whisper-1 pass on a voiced-fraction
check, and keep `_detect_hallucination` as the backstop. This module only
produces the stem.
"""

from __future__ import annotations

import logging
import os
import tempfile

logger = logging.getLogger("genly.vocal_sep")

_TRUE = ("1", "true", "yes", "on", "y", "t")

# Replicate demucs model. Pinned + verified live 2026-05-22 via Replicate API
# (account tometh22); inputs confirmed to include `audio` + `stem`. Override
# via DEMUCS_MODEL if you switch to another separation model.
_MODEL = os.environ.get(
    "DEMUCS_MODEL",
    "cjwbw/demucs:25a173108cff36ef9f80f854c162d01df9e6528be175794b81158fa03836d953",
)

# Internal demucs variant. Default `mdx_extra`: the paper arXiv 2506.15514
# specifically measured WER 47.2%→27.7% with this checkpoint (higher quality
# than the `htdemucs` default — slower, but latency does not matter to us).
# Other options exposed by cjwbw/demucs's `model_name` input: `htdemucs`,
# `htdemucs_ft`, `mdx_extra_q`, `mdx`, `mdx_q`.
_VARIANT = os.environ.get("DEMUCS_VARIANT", "mdx_extra")


def is_enabled() -> bool:
    """On only when the flag is set AND a Replicate token is present."""
    flag = os.environ.get("VOCAL_SEP_ENABLED", "0").strip().lower() in _TRUE
    return flag and bool(os.environ.get("REPLICATE_API_TOKEN", "").strip())


def _pick_vocals(output) -> object | None:
    """Pull the vocal stem out of demucs' output, which varies by model
    version: a dict keyed by stem name, or a single file-ish value."""
    if output is None:
        return None
    if isinstance(output, dict):
        for key in ("vocals", "vocal", "voice"):
            if output.get(key) is not None:
                return output[key]
        return None
    # A bare file/url output (some forks emit only the vocal stem).
    return output


def _download(value, dest_path: str) -> bool:
    """Write a Replicate file output to dest_path. Handles the SDK's
    FileOutput object (`.read()`), a URL string, or an open file-like."""
    try:
        # replicate>=1.0 FileOutput / any object exposing read()
        if hasattr(value, "read"):
            data = value.read()
            with open(dest_path, "wb") as f:
                f.write(data)
            return os.path.getsize(dest_path) > 0
        # URL string
        url = getattr(value, "url", None) or (value if isinstance(value, str) else None)
        if url:
            import urllib.request
            urllib.request.urlretrieve(url, dest_path)
            return os.path.getsize(dest_path) > 0
    except Exception as e:  # network, disk, anything
        logger.warning("[VOCALSEP] download failed (%s)", e)
    return False


def separate_vocals(audio_path: str) -> str | None:
    """Isolate the vocal stem of `audio_path` via demucs on Replicate.
    Returns the path to a temp stem file, or None (disabled / failure).
    Never raises — callers fall back to the mixed audio.
    """
    if not is_enabled():
        return None
    if not audio_path or not os.path.exists(audio_path):
        return None

    try:
        import replicate
    except ImportError:
        logger.warning("[VOCALSEP] replicate SDK not installed — falling back")
        return None

    try:
        with open(audio_path, "rb") as audio:
            output = replicate.run(
                _MODEL,
                input={"audio": audio, "stem": "vocals", "model_name": _VARIANT},
            )
    except Exception as e:  # network, billing, model error, anything
        logger.warning("[VOCALSEP] replicate call failed (%s) — falling back", e)
        return None

    vocals = _pick_vocals(output)
    if vocals is None:
        logger.warning("[VOCALSEP] no vocal stem in output — falling back")
        return None

    fd, dest = tempfile.mkstemp(suffix="_vocals.wav", prefix="genly_stem_")
    os.close(fd)
    if not _download(vocals, dest):
        try:
            os.unlink(dest)
        except OSError:
            pass
        logger.warning("[VOCALSEP] could not save vocal stem — falling back")
        return None

    logger.info("[VOCALSEP] isolated vocal stem for %s", os.path.basename(audio_path))
    return dest
