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
from typing import Callable, Optional

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


def validate_stem(path: str):
    """Sanity-check a vocal stem before handing it downstream (forced_align,
    whisperX). Returns `(ok, reason)`. From PR #281."""
    if not path or not os.path.exists(path):
        return False, "missing"
    try:
        size = os.path.getsize(path)
    except OSError as e:
        return False, f"stat_failed:{e}"
    if size < 10_000:
        return False, f"too_small:{size}B"

    import subprocess
    import json as _json
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_streams", "-show_format", path],
            capture_output=True, text=True, timeout=10, check=True,
        )
        meta = _json.loads(out.stdout)
    except Exception as e:
        return False, f"ffprobe_failed:{e.__class__.__name__}"

    streams = [s for s in (meta.get("streams") or []) if s.get("codec_type") == "audio"]
    if not streams:
        return False, "no_audio_stream"
    st = streams[0]

    try:
        dur = float(
            st.get("duration")
            or (meta.get("format") or {}).get("duration")
            or 0
        )
    except (TypeError, ValueError):
        dur = 0.0
    if dur < 3.0:
        return False, f"duration_too_short:{dur:.2f}s"

    try:
        sr = int(st.get("sample_rate") or 0)
    except (TypeError, ValueError):
        sr = 0
    if sr not in (8000, 11025, 16000, 22050, 24000, 32000, 44100, 48000):
        return False, f"unusual_sample_rate:{sr}"

    try:
        ch = int(st.get("channels") or 0)
    except (TypeError, ValueError):
        ch = 0
    if ch not in (1, 2):
        return False, f"unusual_channels:{ch}"

    return True, "ok"


_REPLICATE_ALLOWED_HOSTS = (
    "replicate.delivery",
    "pbxt.replicate.delivery",
    "tjzk.replicate.delivery",
    "api.replicate.com",
)


def _is_safe_replicate_url(url: str) -> bool:
    """Refuse URLs that aren't on the Replicate CDN. SECURITY: protects
    against SSRF / credential-leak when a model output (or DNS/proxy
    redirect) returns a URL pointing at internal infra. From PR #284."""
    try:
        from urllib.parse import urlparse
        p = urlparse(url)
    except Exception:
        return False
    if p.scheme not in ("https", "http"):
        return False
    host = (p.hostname or "").lower()
    if not host:
        return False
    return any(host == h or host.endswith("." + h) for h in _REPLICATE_ALLOWED_HOSTS)


def _download(value, dest_path: str) -> bool:
    """Write a Replicate file output to dest_path. Handles the SDK's
    FileOutput object (`.read()`), a URL string, or an open file-like.

    SECURITY: when the value is a URL (not a FileOutput), validate it
    points at the Replicate CDN before downloading. A compromised model
    or proxy redirect to `file:///etc/passwd` or an internal-IP target
    would otherwise be fetched and written to disk.
    """
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
            if not _is_safe_replicate_url(url):
                logger.warning("[VOCALSEP] refusing download from non-Replicate host: %s",
                               url[:120])
                return False
            import urllib.request
            urllib.request.urlretrieve(url, dest_path)
            return os.path.getsize(dest_path) > 0
    except Exception as e:  # network, disk, anything
        logger.warning("[VOCALSEP] download failed (%s)", e)
    return False


def _audio_content_hash(audio_path: str) -> str:
    """SHA-256 of the audio file's bytes, first 16 hex chars. Used as
    the cache key for the R2 stem cache. Sub-64-bit space is fine — we
    only need uniqueness within a tenant's catalog (a few thousand
    songs)."""
    import hashlib
    h = hashlib.sha256()
    # Read in chunks to avoid loading a 60 MB WAV into RAM.
    with open(audio_path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _stem_cache_key(audio_path: str) -> str:
    """R2 key for the demucs stem of this audio. Includes the demucs
    variant so different models don't share a cache entry (changing
    `DEMUCS_VARIANT` from `mdx_extra` to `htdemucs` cleanly forces a
    re-separation across the catalog)."""
    return f"stems/{_audio_content_hash(audio_path)}_{_VARIANT}.wav"


def separate_vocals(
    audio_path: str,
    on_progress: Optional[Callable[[float], None]] = None,
) -> str | None:
    """Isolate the vocal stem of `audio_path` via demucs on Replicate.
    Returns the path to a temp stem file, or None (disabled / failure).
    Never raises — callers fall back to the mixed audio.

    PR #300 R2 STEM CACHE: before paying ~60-180 s on Replicate, check
    whether the stem for this audio already lives in R2 under
    `stems/{content_hash}_{variant}.wav`. If yes, download to a local
    tempfile and skip Replicate entirely. Cache misses run demucs
    normally + upload the result for future reuse. Trade-off: R2
    storage cost (~$0.015/GB-month; a 30 MB stem × 100 songs/month
    active ≈ $0.045/month — negligible). Cleanup of unused stems is
    deferred to the reaper.

    `on_progress`: optional callback `(fraction: float) -> None` called
    while Demucs runs on Replicate. Used by the transcription pipeline
    to keep the "Aislando voz" progress bar advancing instead of stuck
    at 25%. On a cache hit it's fired once with 1.0 (no Replicate call
    happens) — the caller's UI still gets a clean transition. None
    keeps the original behaviour (no callbacks).
    """
    if not is_enabled():
        return None
    if not audio_path or not os.path.exists(audio_path):
        return None

    # R2 cache check — before paying Replicate inference cost.
    cache_key = None
    try:
        import storage as _storage
        if _storage.is_enabled():
            cache_key = _stem_cache_key(audio_path)
            if _storage.object_exists(cache_key):
                fd, cached_path = tempfile.mkstemp(suffix="_vocals.wav", prefix="genly_stem_cached_")
                os.close(fd)
                if _storage.download_object(cache_key, cached_path):
                    ok, reason = validate_stem(cached_path)
                    if ok:
                        logger.info("[VOCALSEP] cache hit %s for %s — skipping Replicate",
                                    cache_key, os.path.basename(audio_path))
                        # Cache hit: emit a single 1.0 tick so the UI moves
                        # off 25% and the next stage starts smoothly.
                        if on_progress is not None:
                            try:
                                on_progress(1.0)
                            except Exception:
                                pass
                        return cached_path
                    logger.warning("[VOCALSEP] cached stem failed validation (%s) — re-separating",
                                   reason)
                # Cache miss-ish: download failed or validation failed.
                try:
                    os.unlink(cached_path)
                except OSError:
                    pass
    except Exception as e:
        # Cache check failures must NEVER break the main path.
        logger.warning("[VOCALSEP] cache check failed (%s) — proceeding with Replicate", e)
        cache_key = None  # don't try to upload either

    try:
        import replicate  # noqa: F401 — used inside `call_with_budget`
    except ImportError:
        logger.warning("[VOCALSEP] replicate SDK not installed — falling back")
        return None

    # OBSERVABILITY (audit 2026-05-24): demucs USED to be a single
    # `replicate.run(...)` with no budget. When Replicate was degraded,
    # the HTTP request could hang ~90 minutes before timing out,
    # blocking the worker on the very first step. Same shape as the
    # forced_align bug PR #281 already fixed — extracted to a shared
    # helper so all three call sites stay in sync.
    from replicate_budget import call_with_budget, _budget_for

    # Cada retry del budget llama al factory de nuevo; acumulamos los
    # handles para cerrarlos en `finally`. Sin esto el worker leakea un
    # FD por intento cuando Replicate falla mid-upload (timeout/cancel) —
    # mismo patrón que forced_align.py / whisperx_transcribe.py.
    _open_handles: list = []
    def _input_factory():
        f = open(audio_path, "rb")
        _open_handles.append(f)
        return {
            "audio": f,
            "stem": "vocals",
            "model_name": _VARIANT,
        }

    try:
        output = call_with_budget(
            _MODEL, _input_factory,
            total_budget_s=_budget_for("demucs", default_s=360.0),
            backoff=[0, 8, 24],
            call_label="VOCALSEP",
            on_progress=on_progress,
            # p50 wallclock observed on cjwbw/demucs across the catalog. The
            # time-based fallback uses this when the model logs don't expose
            # a parseable % — the bar advances at the rate a real job would,
            # never reaching 1.0 until Replicate actually says "succeeded".
            typical_runtime_s=90.0,
        )
    finally:
        for _h in _open_handles:
            try:
                _h.close()
            except Exception:
                pass
    if output is None:
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

    # Validate the stem before publishing it to callers. A bad stem
    # (1-sample tensor, wrong sample rate, etc.) crashes forced_align
    # hard — pre-flight here so the caller falls through to the mix and
    # we never burn three retries of ~90 min each on garbage input.
    # Incident: Mujer Amante 2026-05-24 (see validate_stem docstring).
    ok, reason = validate_stem(dest)
    if not ok:
        logger.warning("[VOCALSEP] stem failed validation (%s) for %s — falling back to mix",
                       reason, os.path.basename(audio_path))
        try:
            os.unlink(dest)
        except OSError:
            pass
        return None

    # PR #300 cache-write: upload the fresh stem to R2 under the
    # content-hash key so the next transcription of the same audio
    # skips Replicate entirely. Best-effort — never blocks the return.
    if cache_key:
        try:
            import storage as _storage
            if _storage.is_enabled():
                uploaded = _storage.upload_file(dest, cache_key)
                if uploaded:
                    logger.info("[VOCALSEP] uploaded stem to R2 cache %s", cache_key)
                else:
                    logger.warning("[VOCALSEP] cache upload returned None for %s", cache_key)
        except Exception as e:
            logger.warning("[VOCALSEP] cache upload failed (%s) — stem still usable for this job", e)

    logger.info("[VOCALSEP] isolated vocal stem for %s", os.path.basename(audio_path))
    return dest
