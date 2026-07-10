"""Make moviepy 1.0.3 tolerant of non-UTF-8 bytes in ffmpeg's output.

moviepy's `ffmpeg_parse_infos` (video/io/ffmpeg_reader.py:259) does
`error.decode('utf8')` in STRICT mode on ffmpeg's stderr banner. ffmpeg echoes
the input path — and the file's embedded metadata — in that banner, so when the
name or a tag carries non-UTF-8 bytes (a Spanish title like "La Vida Al Revés"
→ byte 0xe9 for "é"), the decode raises UnicodeDecodeError and the whole render
dies inside `AudioFileClip(...)` / `VideoFileClip(...)`. This hit UMG Chile jobs
on essentially any accented title (Sentry PYTHON-FASTAPI-2N, job 27b099100e7d).

Fix: wrap `ffmpeg_parse_infos` so that, ONLY on UnicodeDecodeError, it retries
against an ASCII-named, metadata-stripped remux of the input. That remux is
produced with a byte-mode subprocess (no text decode), so the fallback step
itself can never hit the bug we're fixing. moviepy's own parser is reused — we
do NOT fork its ~90-line function — and the caller still reads the audio/video
frames from the ORIGINAL file, so nothing else about the render changes. The
happy path (ASCII files) is completely untouched: the wrapper only does work
when the original call raises.

Pinned to moviepy==1.0.3 and defensive: if the real moviepy internals aren't
importable (e.g. the CI stub in tests/conftest.py) or the installed version
differs, `apply_patch()` is a safe no-op so importing this module never breaks
`pipeline.py`.
"""

import logging
import os
import subprocess
import tempfile

logger = logging.getLogger("genly.moviepy_patch")

_EXPECTED_MOVIEPY = "1.0.3"
_applied = False
# Bound to moviepy's real ffmpeg_parse_infos by apply_patch(); left None (and
# the wrapper unused) when moviepy internals are unavailable.
_ORIGINAL_PARSE_INFOS = None


def _ffmpeg_binary():
    """The same ffmpeg binary moviepy itself uses (imageio-ffmpeg by default)."""
    try:
        from moviepy.config import get_setting
        return get_setting("FFMPEG_BINARY")
    except Exception:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()


# Matroska accepts virtually any codec (PCM, MP3, AAC, FLAC, Opus, H.264…) via
# `-c copy`, so remuxing into it can't fail on a container/codec mismatch. We do
# NOT infer the container from `src`'s extension: run_edit_pipeline saves the
# input as `source_audio.mp3` regardless of the real codec, so a WAV upload lands
# as PCM-bytes-named-.mp3 — and `-c copy` into an .mp3 container then dies with
# ffmpeg exit 234 (the UMG Chile edit incident, 2026-07-09). A fixed .mkv target
# sidesteps that entirely while still stripping metadata to an ASCII-named file.
_SAFE_COPY_CONTAINER = ".mkv"


def _ascii_metadata_free_copy(src):
    """Remux `src` to an ASCII-named, metadata-free temp file so ffmpeg's banner
    for it is pure ASCII.

    Uses a universal Matroska container (so the remux can't fail on a
    container/codec mismatch when `src`'s extension lies about its real codec)
    and a byte-mode subprocess (stderr captured as bytes, never strict-decoded)
    so this step cannot itself raise the UnicodeDecodeError we are working around.

    On an actual ffmpeg failure the temp file is removed and the original
    CalledProcessError is re-raised — with ffmpeg's stderr attached — so the job
    surfaces a real "render" error instead of a cryptic bare exit code, and the
    error taxonomy still classifies it correctly.
    """
    fd, dst = tempfile.mkstemp(suffix=_SAFE_COPY_CONTAINER)
    os.close(fd)
    try:
        subprocess.run(
            [_ffmpeg_binary(), "-y", "-i", src,
             "-map_metadata", "-1", "-c", "copy", dst],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or b"").decode("utf-8", "replace")[-800:]
        logger.warning(
            "[moviepy-patch] metadata-free remux of %r failed (exit %s): %s",
            src, exc.returncode, stderr,
        )
        try:
            os.remove(dst)
        except OSError:
            pass
        raise
    return dst


def _tolerant_parse_infos(filename, *args, **kwargs):
    """Drop-in for moviepy's ffmpeg_parse_infos that survives non-UTF-8 bytes
    in ffmpeg's stderr banner (accented filename / embedded metadata)."""
    try:
        return _ORIGINAL_PARSE_INFOS(filename, *args, **kwargs)
    except UnicodeDecodeError:
        safe = _ascii_metadata_free_copy(filename)
        try:
            return _ORIGINAL_PARSE_INFOS(safe, *args, **kwargs)
        finally:
            try:
                os.remove(safe)
            except OSError:
                pass


def apply_patch():
    """Rebind moviepy's ffmpeg_parse_infos to the tolerant version.

    Returns True if the patch is active, False if it was (safely) skipped —
    e.g. moviepy internals unavailable (CI stub) or an unexpected version.
    """
    global _applied, _ORIGINAL_PARSE_INFOS
    if _applied:
        return True

    try:
        import moviepy
        import moviepy.audio.io.readers as audio_readers
        import moviepy.video.io.ffmpeg_reader as ffmpeg_reader
    except Exception as exc:  # CI stub / partial install — never break imports
        logger.debug("[moviepy-patch] moviepy internals unavailable (%s) — not patching", exc)
        return False

    version = getattr(moviepy, "__version__", None)
    if version is None:
        try:
            from moviepy.version import __version__ as version
        except Exception:
            version = None
    if version != _EXPECTED_MOVIEPY:
        logger.warning(
            "[moviepy-patch] moviepy %s != pinned %s — UTF-8 tolerance NOT "
            "applied; re-check ffmpeg_parse_infos before bumping.",
            version, _EXPECTED_MOVIEPY,
        )
        return False

    # Idempotent even if some other import path patched already.
    if getattr(ffmpeg_reader.ffmpeg_parse_infos, "_genly_utf8_patched", False):
        _applied = True
        return True

    _ORIGINAL_PARSE_INFOS = ffmpeg_reader.ffmpeg_parse_infos
    _tolerant_parse_infos._genly_utf8_patched = True
    ffmpeg_reader.ffmpeg_parse_infos = _tolerant_parse_infos
    # readers.py did `from ...ffmpeg_reader import ffmpeg_parse_infos`, so it
    # holds its own name binding that must be rebound too.
    audio_readers.ffmpeg_parse_infos = _tolerant_parse_infos
    _applied = True
    logger.info("[moviepy-patch] ffmpeg_parse_infos now tolerant of non-UTF-8 ffmpeg output")
    return True


# Self-apply on import so a plain `import moviepy_utf8_patch` at the top of
# pipeline.py is enough to arm it before any clip is opened.
apply_patch()
