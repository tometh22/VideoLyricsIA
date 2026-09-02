"""Bounded worker-side generation of the editor's seekable audio preview.

This module is intentionally independent from transcription and rendering. It
only creates a derivative for the browser editor; all final video/audio work
continues to consume the immutable ``input_r2_key`` master.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile

import storage

logger = logging.getLogger("genly.audio_preview")

EDITOR_AUDIO_PREVIEW_DRIFT_LIMIT_SECONDS = 0.050
EDITOR_AUDIO_PREVIEW_FFMPEG_TIMEOUT_SECONDS = int(
    os.environ.get("EDITOR_AUDIO_PREVIEW_FFMPEG_TIMEOUT_SECONDS", "600")
)


def _probe_duration(path: str) -> float:
    """Read container duration without decoding the media."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", path,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    duration = float((result.stdout or "").strip())
    if duration <= 0:
        raise RuntimeError("audio_preview_invalid_duration")
    return duration


def _transcode(input_path: str, output_path: str) -> None:
    """Create an AAC-LC stereo M4A with an early-playback moov atom."""
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", input_path,
            "-map", "0:a:0", "-vn",
            "-c:a", "aac", "-profile:a", "aac_low", "-b:a", "96k",
            "-ac", "2",
            "-movflags", "+faststart",
            # Audio encoding is cheap but this prevents one preview from
            # consuming every CPU on a shared worker.
            "-threads", "2",
            output_path,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=EDITOR_AUDIO_PREVIEW_FFMPEG_TIMEOUT_SECONDS,
    )


def run_editor_audio_preview_job(
    input_r2_key: str,
    audio_sha256: str,
    preview_r2_key: str,
    lock_token: str = "",
) -> dict:
    """Materialize one content-addressed preview, idempotently.

    The lock token is supplied by ``queue_jobs`` and is released even when a
    download, ffmpeg, validation, or upload fails. A failed job therefore
    leaves the original source usable and allows a later editor request to
    retry generation.
    """
    try:
        if storage.object_exists(preview_r2_key):
            return {"status": "exists", "preview_key": preview_r2_key}

        with tempfile.TemporaryDirectory(prefix="editor-audio-preview-") as tmp:
            input_path = os.path.join(tmp, "source.audio")
            output_path = os.path.join(tmp, "preview.part.m4a")
            if not storage.download_object(input_r2_key, input_path):
                raise RuntimeError("audio_preview_source_download_failed")

            # The source key is tenant/job scoped, but the destination is
            # shared. Never publish bytes under digest A if the input object
            # was replaced or the queue payload was malformed.
            from quality_cache import sha256_file
            actual_sha256 = sha256_file(input_path)
            if actual_sha256 != str(audio_sha256 or "").strip().lower():
                raise RuntimeError("audio_preview_source_digest_mismatch")

            source_duration = _probe_duration(input_path)
            _transcode(input_path, output_path)
            preview_duration = _probe_duration(output_path)
            drift = abs(preview_duration - source_duration)
            if drift >= EDITOR_AUDIO_PREVIEW_DRIFT_LIMIT_SECONDS:
                raise RuntimeError(
                    f"audio_preview_duration_drift:{drift:.6f}"
                )

            if storage.upload_file(output_path, preview_r2_key) != preview_r2_key:
                raise RuntimeError("audio_preview_upload_failed")
            # Do not report success until the final object is visible. R2 is
            # strongly consistent, and this also catches mocked/partial
            # upload implementations before the API signs a broken URL.
            if not storage.object_exists(preview_r2_key):
                raise RuntimeError("audio_preview_uploaded_object_missing")

            logger.info(
                "[EDITOR-AUDIO-PREVIEW] ready digest=%s duration=%.3fs drift=%.3fs",
                str(audio_sha256)[:12], source_duration, drift,
            )
            return {
                "status": "ready",
                "preview_key": preview_r2_key,
                "duration": source_duration,
                "drift": drift,
            }
    except subprocess.CalledProcessError as exc:
        # Keep logs useful without dumping input paths, signed URLs, or a
        # potentially very large ffmpeg stderr payload.
        detail = (exc.stderr or "").strip().splitlines()[-1:]
        logger.warning(
            "[EDITOR-AUDIO-PREVIEW] ffmpeg failed digest=%s detail=%s",
            str(audio_sha256)[:12], detail[0][:240] if detail else "unknown",
        )
        raise
    finally:
        if lock_token:
            try:
                from queue_jobs import release_editor_audio_preview_lock
                release_editor_audio_preview_lock(audio_sha256, lock_token)
            except Exception:
                logger.warning(
                    "[EDITOR-AUDIO-PREVIEW] lock release failed digest=%s",
                    str(audio_sha256)[:12],
                )
