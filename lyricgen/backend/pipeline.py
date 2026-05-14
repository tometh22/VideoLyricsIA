"""Full processing pipeline: Whisper → Video → Short → Thumbnail."""

import hashlib
import json
import os
import math
import random
import re
import subprocess
import tempfile
import traceback

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

import librosa
import numpy as np
from PIL import Image as _PILImage
if not hasattr(_PILImage, "ANTIALIAS"):
    _PILImage.ANTIALIAS = _PILImage.LANCZOS
from moviepy.config import change_settings

# Auto-detect ImageMagick binary (v7 uses "magick", v6 uses "convert")
for _candidate in [
    "/opt/homebrew/bin/magick",
    "/usr/local/bin/magick",
    "/usr/bin/magick",
    "/opt/homebrew/bin/convert",
    "/usr/local/bin/convert",
    "/usr/bin/convert",
]:
    if os.path.exists(_candidate):
        change_settings({"IMAGEMAGICK_BINARY": _candidate})
        break

from moviepy.editor import (
    AudioFileClip,
    ColorClip,
    CompositeVideoClip,
    TextClip,
    VideoClip,
    VideoFileClip,
    concatenate_videoclips,
)
from PIL import Image, ImageDraw, ImageFont

from jobs import update_job, get_job
import storage
from render_spec import FPS_RATIONAL, RenderSpec

ASSETS_DIR = os.path.join(os.path.dirname(__file__), "..", "assets")
OUTPUTS_DIR = os.path.join(os.path.dirname(__file__), "..", "outputs")
BACKGROUNDS_DIR = os.path.join(ASSETS_DIR, "backgrounds")


_DELIVERABLE_FILENAMES = {
    "video": "lyric_video.mp4",
    "short": "short.mp4",
    "thumbnail": "thumbnail.jpg",
    "umg_master": "umg_master.mov",
    "umg_short": "umg_short.mov",
}


def _upload_deliverables_to_r2(job_id: str, job_dir: str, files: dict) -> dict:
    """Upload each produced deliverable to R2 and delete the local copy on
    success. Returns {file_type: s3_key}.

    Non-fatal on upload errors — the local file stays and the job still
    reports done. Failed uploads will be retried by a later cleanup pass.
    """
    if not storage.is_enabled():
        return {}
    # get_job() requires a SQLAlchemy session, but this function runs in the
    # worker context with no request-scoped session available. Create one
    # here just to look up the tenant_id, then close it. (We could pass
    # tenant_id in from the caller, but the call site already has the job_id
    # and we need the cheap row read.)
    from database import SessionLocal
    _db = SessionLocal()
    try:
        job = get_job(_db, job_id)
    finally:
        _db.close()
    tenant_id = (job or {}).get("tenant_id", "default")
    out: dict = {}
    for file_type, _url in files.items():
        key_name = _DELIVERABLE_FILENAMES.get(file_type.replace("_url", ""))
        if not key_name:
            continue
        local = os.path.join(job_dir, key_name)
        if not os.path.exists(local):
            continue
        try:
            key = storage.upload_master(local, tenant_id, job_id, key_name)
            if key:
                out[file_type.replace("_url", "")] = key
                # Upload confirmed — delete the local copy so the disk doesn't
                # fill up (a HD ProRes master is ~5 GB, a 240 GB NVMe fills
                # after ~50 UMG deliveries).
                try:
                    os.unlink(local)
                except OSError as e:
                    print(f"[R2] Could not remove local {local}: {e}")
        except Exception as e:
            print(f"[R2] Upload failed for {key_name}: {e}")
    return out


def _ffprobe_duration(path: str) -> float | None:
    """Return media duration in seconds, or None if ffprobe fails."""
    import subprocess
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=30,
        )
        return float((r.stdout or "").strip())
    except Exception:
        return None


def _verify_deliverables(job_dir: str, files: dict, audio_duration: float) -> None:
    """Sanity-check every deliverable BEFORE the R2 upload.

    Catches the silent-failure family:
    - ffmpeg exited 0 but produced an empty/truncated file (disk full, OOM
      mid-flush)
    - moviepy crashed mid-render and left the prior pass's leftover file
    - duration mismatch (audio offset bug, encoder cut early)
    - codec mismatch (caller forgot to pass the right RenderSpec)

    Raises RuntimeError on any failure so the outer try/except in
    run_pipeline marks the job 'error' with a clear message instead of
    uploading garbage to R2 + shipping it to UMG.
    """
    import os as _os

    expected = {
        "video_url":      ("lyric_video.mp4", "h264", audio_duration),
        "short_url":      ("short.mp4",        "h264", None),  # short is a fixed clip, not full audio
        "thumbnail_url":  ("thumbnail.jpg",   None,   None),
        # umg_master is generated lazily at download time via ffmpeg from
        # the MP4 above (see /download/{id}/umg_master). It does NOT
        # exist on disk after the pipeline finishes, so we don't verify
        # it here — the download endpoint validates the .mov post-
        # transcode using _validate_umg_master.
    }
    for url_key, (filename, expected_codec, expected_dur) in expected.items():
        if url_key not in files:
            continue
        path = _os.path.join(job_dir, filename)
        if not _os.path.exists(path):
            raise RuntimeError(f"verify: {filename} missing on disk after render")
        size = _os.path.getsize(path)
        if size < 1024:
            raise RuntimeError(f"verify: {filename} is {size} bytes (truncated / empty)")

        if expected_codec:
            # ffprobe codec check
            import subprocess
            r = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=codec_name",
                 "-of", "default=noprint_wrappers=1:nokey=1", path],
                capture_output=True, text=True, timeout=30,
            )
            codec = (r.stdout or "").strip()
            if codec != expected_codec:
                raise RuntimeError(
                    f"verify: {filename} codec is {codec!r}, expected {expected_codec!r}"
                )

        if expected_dur is not None:
            actual_dur = _ffprobe_duration(path)
            if actual_dur is None:
                raise RuntimeError(f"verify: {filename} ffprobe could not read duration")
            # ±2s tolerance — encoder rounding + container overhead
            if abs(actual_dur - expected_dur) > 2.0:
                raise RuntimeError(
                    f"verify: {filename} duration {actual_dur:.1f}s differs from "
                    f"audio {expected_dur:.1f}s by > 2s"
                )

    print(f"[VERIFY] all {len(files)} deliverables passed sanity checks "
          f"(umg_master, if requested, is generated lazily at download)")


def _cleanup_local_intermediates(job_dir: str) -> None:
    """Drop intermediate render artefacts that are not deliverables. Keeps the
    directory + any leftover deliverable that R2 upload missed, so the job
    can still be recovered."""
    leftovers = ("bg_generated.mp4", "bg_gradient_fallback.mp4")
    for name in leftovers:
        path = os.path.join(job_dir, name)
        if os.path.exists(path):
            try:
                os.unlink(path)
            except OSError:
                pass
    # Also drop any per-spec looped backgrounds (bg_looped_*.mp4)
    try:
        for entry in os.listdir(job_dir):
            if entry.startswith("bg_looped_") and entry.endswith(".mp4"):
                try:
                    os.unlink(os.path.join(job_dir, entry))
                except OSError:
                    pass
    except OSError:
        pass


def _get_persisted_segments(job_id: str) -> list[dict] | None:
    """Return job_row.segments_json if it's a non-empty list, else None.

    Used by run_pipeline's "preserve user edits across retries" branch.
    Opens its own short-lived DB session so the caller doesn't have to
    pass one in (matching the rest of pipeline.py's update_job pattern).
    Best-effort: any exception returns None and the caller falls back to
    a fresh Whisper transcription — we never want a DB hiccup to
    silently produce a worse video.
    """
    try:
        from database import SessionLocal, Job
        with SessionLocal() as db:
            row = db.query(Job).filter(Job.job_id == job_id).first()
            if row is None:
                return None
            segs = row.segments_json
            if not segs or not isinstance(segs, list) or len(segs) == 0:
                return None
            return segs
    except Exception as e:  # pragma: no cover
        print(f"[PIPELINE] _get_persisted_segments({job_id}) failed: {e}")
        return None


def _best_effort_lyrics_hint(artist: str, song_title: str) -> str | None:
    """Fetch the reference lyrics from the Gemini-grounded search cache
    (or fresh search) to use as Whisper's `prompt` parameter.

    Why this exists: `/transcribe` already does this on the upload path,
    biasing Whisper toward the song's actual vocabulary. The pipeline
    path (run_pipeline → transcribe) used to skip the hint, so the same
    audio produced WORSE transcriptions on retry than on first upload.
    The user surfaced this as "el upload fresco siempre acierta, el
    retry alucina" — root cause was just this missing hint.

    Best-effort: returns None on any error so the caller transcribes
    without bias rather than crashing.
    """
    if not artist or not song_title:
        return None
    try:
        from database import SessionLocal
        with SessionLocal() as db:
            return _fetch_lyrics_via_gemini_search(
                artist, song_title, job_id=None, db=db,
            )
    except Exception as e:  # pragma: no cover
        print(f"[PIPELINE] lyrics_hint fetch failed: {e}")
        return None


def run_pipeline(job_id: str, mp3_path: str, artist: str, style: str,
                 language: str = None, segments_override: list[dict] = None,
                 delivery_profile: str = "youtube", umg_spec: dict | None = None,
                 background_path: str = None,
                 input_r2_key: str | None = None,
                 bg_r2_key: str | None = None,
                 variation_source_path: str | None = None,
                 variation_source_r2_key: str | None = None,
                 variation_parent_asset_id: int | None = None,
                 genre: str = "",
                 font: str = "",
                 concept: str = "",
                 movement_style: str = "",
                 animate_image: bool = False,
                 song_title: str = "",
                 text_case: str = "upper",
                 font_scale: float = 1.0,
                 lyric_transition: str = "cut",
                 text_motion: str = "none",
                 match_lyrics: bool = True,
                 text_contrast: str = "medium",
                 # Background_hint llega solo desde el flow de variantes
                 # (POST /jobs/{id}/variant). En el upload normal viene
                 # vacío y el prompt Gemini se arma 100% desde concept +
                 # genre + lyrics. Cuando viene set, _ensure_background
                 # lo inyecta como [OPERATOR OVERRIDE] en el user_content
                 # de Gemini, misma mecánica que /edit (PR #116).
                 background_hint: str | None = None):
    """Run the full pipeline for a job. Called synchronously.

    delivery_profile:
        "youtube" — YouTube MP4 + short + thumbnail (default).
        "umg"     — UMG ProRes master only (no short/thumbnail).
        "both"    — YouTube bundle + UMG master, sharing bg/font.

    background_path:
        If provided, skip AI background generation and use the human-provided
        asset instead (UMG Guideline 10 compliance).

    input_r2_key / bg_r2_key:
        When the API and worker run in separate containers (e.g. Railway), the
        local mp3_path / background_path written by the API are NOT visible
        to the worker. The API uploads the input MP3 (and any custom
        background) to R2 and passes the keys here; we download them locally
        before processing, restoring the same file paths the rest of the
        pipeline expects.

    variation_source_path / variation_source_r2_key / variation_parent_asset_id:
        Set when the user picked a library asset in "variation" mode. We
        materialize the source video, extract a representative frame, and
        feed it to Veo as image-to-video input — Veo then generates a
        brand-new clip visually derived from the original. This is how UMG
        gets a unique video off a library asset without needing a real
        video-to-video model (Veo 3.1 only supports image-to-video).
    """
    job_dir = os.path.join(OUTPUTS_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    # Worker just claimed this job — flip the user-facing status from
    # "queued" (sitting in RQ) to "processing" (a worker is actively on it).
    # This makes the queue visible in the dashboard: jobs piling up in RQ
    # show as "queued" until a worker picks them, at which point they
    # immediately go "processing". Idempotent — if the job already says
    # processing (e.g. on retry), the update is a no-op.
    update_job(job_id, status="processing", current_step="starting", progress=1)

    # Materialize R2-stored inputs onto local disk so moviepy/ffmpeg/whisper
    # can open them. No-op when running on a single host (no R2 keys passed).
    #
    # The /retry endpoint (main.py:retry_job) calls enqueue_pipeline with
    # mp3_path=None because the audio only lives in R2 — the caller has
    # no local path to hand us. Detect that and derive one from the R2
    # key's basename. Without this, os.path.exists(None) raises:
    #   TypeError: stat: path should be string, bytes, os.PathLike or
    #   integer, not NoneType
    # which RQ surfaces to pipeline_failure_callback and the user gets
    # "El render falló tras reintentos" — same row, same retry loop.
    # Discovered 2026-05-11 22:43 UTC when admin retried two jobs that
    # had been reaped from a 4K render stall.
    if mp3_path is None and input_r2_key:
        mp3_path = os.path.join(job_dir, os.path.basename(input_r2_key))
    if input_r2_key and not os.path.exists(mp3_path):
        if not storage.download_object(input_r2_key, mp3_path):
            update_job(
                job_id, status="error",
                error=f"Failed to fetch input from R2: {input_r2_key}",
            )
            return
    if bg_r2_key and background_path and not os.path.exists(background_path):
        if not storage.download_object(bg_r2_key, background_path):
            update_job(
                job_id, status="error",
                error=f"Failed to fetch background from R2: {bg_r2_key}",
            )
            return

    # Variation mode: materialize the source library video locally and
    # extract a frame to use as the Veo image-to-video seed. The source
    # video itself is NOT used as the final background — we only borrow
    # one frame so Veo can derive a visually similar but distinct clip.
    variation_seed_image = None
    if variation_source_path:
        if variation_source_r2_key and not os.path.exists(variation_source_path):
            os.makedirs(os.path.dirname(variation_source_path) or ".", exist_ok=True)
            if not storage.download_object(variation_source_r2_key, variation_source_path):
                update_job(
                    job_id, status="error",
                    error=f"Failed to fetch variation source from R2: {variation_source_r2_key}",
                )
                return
        if not os.path.exists(variation_source_path):
            update_job(
                job_id, status="error",
                error=f"Variation source not found locally: {variation_source_path}",
            )
            return
        variation_seed_image = os.path.join(job_dir, "variation_seed.png")
        try:
            _extract_frame_from_video(variation_source_path, variation_seed_image)
        except Exception as e:
            update_job(
                job_id, status="error",
                error=f"Failed to extract frame for variation: {e}",
            )
            return
        # Hand the extracted frame to the existing image-to-video branch.
        # `_animate_user_image` (computed below) only fires when the
        # background file is a JPG/PNG — our extracted frame is a PNG, so
        # the existing logic will pass it to Veo as the image-to-video
        # seed and produce a brand-new clip derived from it.
        background_path = variation_seed_image
        animate_image = True
        print(f"[BG] variation: seeded Veo image-to-video from frame of "
              f"{os.path.basename(variation_source_path)} (parent asset "
              f"id={variation_parent_asset_id})")

    wants_youtube = delivery_profile in ("youtube", "both")
    wants_umg = delivery_profile in ("umg", "both")

    try:
        # Step 1 — Whisper transcription (or reuse persisted segments).
        # Precedence:
        #   1. Caller-passed segments_override (e.g. /generate after the
        #      wizard's lyrics editor)
        #   2. Job row's segments_json (e.g. /retry path — preserves the
        #      user's previous corrections instead of re-running Whisper
        #      and clobbering them, which is the bug observed on 2026-05-
        #      11 when admin retried after a deploy and lost their lyric
        #      edits)
        #   3. Fresh Whisper transcription (first-ever processing of this
        #      audio). Pass lyrics_hint sourced from artist+song so
        #      Whisper biases toward the right vocabulary — same trick
        #      /transcribe uses on the upload path, restored here so the
        #      retry path matches its quality.
        update_job(job_id, current_step="whisper", progress=5)
        _persist_segments = True
        if segments_override:
            segments = segments_override
            print(f"[WHISPER] Using {len(segments)} caller-supplied segments")
        else:
            # Re-fetch the job row in case the caller (retry) didn't
            # pass us segments but the row has them from a previous
            # generate. update_job above already opened a session so we
            # do this in a fresh, short-lived one.
            persisted = _get_persisted_segments(job_id)
            if persisted:
                segments = persisted
                _persist_segments = False  # don't rewrite identical data
                print(f"[WHISPER] Reusing {len(segments)} persisted segments "
                      f"(skip Whisper — preserves user corrections)")
            else:
                lyrics_hint = _best_effort_lyrics_hint(artist, song_title)
                segments = transcribe(
                    mp3_path, language=language, lyrics_hint=lyrics_hint,
                    job_id=job_id,
                )
        # Persist segments so edit re-renders can skip re-transcription.
        # Skip when we just read them from the same row — pointless write.
        if _persist_segments:
            update_job(job_id, segments_json=segments, progress=20)
        else:
            update_job(job_id, progress=20)

        # Step 1.5 — Background (AI-generated or human-provided)
        update_job(job_id, current_step="background", progress=22)
        # Decide if the operator's upload is a still image they want
        # animated by Veo image-to-video (vs. used as-is via Ken Burns).
        # Path requires: animate_image flag set + background_path is a
        # JPG/PNG (NOT an MP4 — those are already video).
        _is_still = (background_path and
                     background_path.lower().endswith((".jpg", ".jpeg", ".png")))
        _animate_user_image = bool(animate_image and _is_still)

        if background_path and not _animate_user_image:
            # Human-provided background — skip AI generation (UMG Guideline 10)
            from provenance import record_ai_call
            recorder = record_ai_call(
                job_id=job_id,
                step="background_human",
                tool_name="human-provided",
                tool_provider="user_upload",
                prompt="User-uploaded background asset (no AI generation)",
                input_data_types=["user_uploaded_file"],
            )
            recorder.finish(
                response_summary="human_provided_background",
                output_artifact=background_path,
            )
            bg_image_path = background_path
            print(f"[BG] Using human-provided background: {background_path}")
        else:
            lyrics_text = " ".join(seg["text"] for seg in segments)
            # Prefer the structured title the operator set on the job; fall
            # back to filename parsing for legacy rows / batch uploads. The
            # cache key downstream uses (artist|title) as a namespace so
            # different songs don't share a Veo background.
            if song_title:
                _song_title = song_title
            else:
                _basename = os.path.splitext(os.path.basename(mp3_path))[0]
                if " - " in _basename:
                    _song_title = _basename.split(" - ", 1)[1]
                elif "_" in _basename:
                    _song_title = _basename.split("_", 1)[0]
                else:
                    _song_title = _basename
            for _sfx in ["(Official Video)", "(Official Audio)", "(Lyric Video)",
                         "(Official Music Video)", "(En Vivo)", "(Live)", "(Lyrics)"]:
                _song_title = _song_title.replace(_sfx, "").strip()
            if _animate_user_image:
                print(f"[BG] image-to-video: animating user-supplied "
                      f"{os.path.basename(background_path)} via Veo")
            bg_image_path = _ensure_background(
                style, job_dir,
                lyrics_text=lyrics_text, artist=artist, job_id=job_id,
                song_title=_song_title, genre=genre, concept=concept,
                movement_style=movement_style,
                image_to_video_path=(background_path if _animate_user_image else None),
                match_lyrics=match_lyrics,
                background_hint=background_hint,
            )
            # Image-to-video fallback: if Veo failed to produce an MP4 (None
            # or non-existent path) AND the operator wanted to animate their
            # image, fall back to using the still image with Ken Burns.
            if _animate_user_image and (not bg_image_path or not os.path.exists(bg_image_path)):
                print(f"[BG] image-to-video failed, falling back to Ken Burns "
                      f"on {background_path}")
                bg_image_path = background_path
        update_job(job_id, progress=40)

        # Persist render params so edit re-renders can override individual
        # fields without losing the rest of the original settings.
        update_job(job_id, render_params={
            "font": font,
            "text_case": text_case,
            "font_scale": font_scale,
            "lyric_transition": lyric_transition,
            "text_motion": text_motion,
            "style": style,
            "genre": genre,
            "concept": concept,
            "movement_style": movement_style,
        })

        # Cache AI-generated background to R2 so a typography-only edit
        # can re-use it without another Veo call ($0.80 saved per edit).
        # Only worth doing when storage is available and the background is
        # a file we own (not a human-provided upload — those already have
        # their own R2 key in bg_r2_key).
        if bg_image_path and os.path.exists(bg_image_path) and not background_path:
            import storage as _storage
            if _storage.is_enabled():
                try:
                    _bg_ext = os.path.splitext(bg_image_path)[1] or ".mp4"
                    _bg_cache_key = _storage.upload_file(
                        bg_image_path,
                        f"backgrounds/{job_id}/bg_cached{_bg_ext}",
                    )
                    if _bg_cache_key:
                        update_job(job_id, bg_r2_key_cached=_bg_cache_key)
                        print(f"[EDIT] Cached background to R2: {_bg_cache_key}")
                except Exception as _e:
                    print(f"[EDIT] Warning: background cache upload failed: {_e}")

        files = {}
        # When the operator picked an explicit font id, resolve it to a
        # path now and seed `chosen_font`. generate_lyric_video reuses a
        # truthy `font` argument as-is and only random-picks when None.
        chosen_font = _resolve_font(font)
        if chosen_font:
            print(f"[FONT] Operator-selected: {os.path.basename(chosen_font)}")
        bg_source = bg_image_path

        # Step 1b — Pre-render content validation (UMG Guideline 15).
        # We validate the BACKGROUND ASSET BEFORE the expensive render so
        # we don't burn 5+ minutes of CPU only to throw the result away.
        # Two paths:
        #   - Operator/AI supplied a specific bg → validate it; if it
        #     fails, mark validation_failed (we can't auto-substitute).
        #   - No bg supplied → cycle through library candidates, picking
        #     the first one that passes (up to 3 attempts).
        if wants_youtube:
            update_job(job_id, current_step="validation", progress=38)
            if bg_image_path:
                from content_validator import validate_video, validate_image
                ext = os.path.splitext(bg_image_path)[1].lower()
                _validate_fn = (
                    validate_video if ext in (".mp4", ".mov", ".webm")
                    else validate_image
                )
                pre_validation = _validate_fn(bg_image_path, job_id=job_id)
                update_job(job_id, validation_result=pre_validation)
                if not pre_validation["passed"]:
                    update_job(
                        job_id,
                        status="validation_failed",
                        error=f"Content policy violation detected: {pre_validation['issues']}",
                    )
                    print(f"[VALIDATION] FAILED for job {job_id}: {pre_validation['issues']}")
                    return
            else:
                clean_bg, rejection_log = _select_validated_background(job_id)
                if not clean_bg:
                    update_job(
                        job_id,
                        status="validation_failed",
                        error=(
                            "No clean background found after retries. "
                            f"Rejections: {rejection_log}"
                        ),
                    )
                    print(f"[VALIDATION] FAILED for job {job_id}: no clean bg after retries")
                    return
                bg_image_path = clean_bg
                update_job(job_id, validation_result={
                    "passed": True, "issues": [], "rejections": rejection_log,
                })

        # Step 2 — Render the source MP4 (H.264 yuv420p aac mp4).
        # Always rendered when ANY delivery profile is requested. The UMG
        # ProRes is generated lazily at download time from this MP4
        # (see _transcode_to_prores + /download/{id}/umg_master) so we
        # avoid the dual-render moviepy-palindrome hang.
        #
        # WHEN UMG IS REQUESTED, render the MP4 at the EXACT UMG target
        # dimensions and fps (still cheap codec). The lazy ProRes
        # transcode then becomes a pure codec/audio/container swap — no
        # ffmpeg scale, no fps interpolation, no chroma stretch. This
        # is what makes the master pass UMG manual QC for any of the
        # 4 frame sizes × 8 fps the spec sheet allows.
        if wants_youtube or wants_umg:
            if wants_umg and not umg_spec:
                raise RuntimeError("UMG delivery requested without umg_spec")
            update_job(job_id, current_step="video", progress=40)
            intermediate_spec = (
                RenderSpec.umg_intermediate_master(umg_spec) if wants_umg
                else None  # generate_lyric_video defaults to youtube_default
            )
            _, chosen_font, bg_source = generate_lyric_video(
                mp3_path, segments, style, job_dir, artist, bg_image_path,
                font=chosen_font, spec=intermediate_spec,
                song_title=song_title,
                text_case=text_case,
                font_scale=font_scale,
                lyric_transition=lyric_transition,
                text_motion=text_motion,
                text_contrast=text_contrast,
            )
            files["video_url"] = f"/download/{job_id}/video"
            update_job(job_id, progress=55)

        # Lazy ProRes — register the URLs so the UI shows the
        # "Master ProRes" + "Short ProRes" download buttons. The
        # actual .mov files are generated on the first GET
        # /download/{id}/umg_master or /download/{id}/umg_short
        # from the existing MP4 / short.mp4 via ffmpeg (no moviepy
        # involvement).
        if wants_umg:
            files["umg_master_url"] = f"/download/{job_id}/umg_master"
            files["umg_short_url"] = f"/download/{job_id}/umg_short"

        # Step 3 — Short (1080×1920 vertical). Same fps as the master
        # when UMG-bound so the lazy ProRes short is also a pure recode.
        if wants_youtube or wants_umg:
            update_job(job_id, current_step="short", progress=75)
            short_fps = float(umg_spec["fps"]) if wants_umg else 24
            generate_short(
                mp3_path, segments, job_dir, bg_source=bg_source,
                style=style, font=chosen_font, fps=short_fps,
            )
            files["short_url"] = f"/download/{job_id}/short"
            update_job(job_id, progress=85)

            # Step 4 — Thumbnail (uses raw background, not lyric video)
            update_job(job_id, current_step="thumbnail", progress=90)
            generate_thumbnail(
                artist, mp3_path, job_dir, bg_source=bg_source,
                song_title=song_title,
            )
            files["thumbnail_url"] = f"/download/{job_id}/thumbnail"

        # Content validation already happened pre-render (Step 1b) so the
        # background here is guaranteed clean. No post-render check needed.

        # Sanity-check every deliverable before uploading to R2. Catches
        # silent failures (truncated files, codec mismatches, duration
        # drift) so we mark the job as error here instead of shipping
        # garbage to UMG.
        try:
            audio_dur_for_verify = _audio_duration(mp3_path)
        except Exception:
            audio_dur_for_verify = None
        if audio_dur_for_verify is None:
            # _audio_duration uses mutagen/wave; fall back to ffprobe for any format
            audio_dur_for_verify = _ffprobe_duration(mp3_path)
        _verify_deliverables(job_dir, files, audio_dur_for_verify)

        # Post-render upload to cloud storage. No-op if R2 env not set.
        s3_keys = _upload_deliverables_to_r2(job_id, job_dir, files)
        if s3_keys:
            update_job(job_id, s3_keys=s3_keys)

        # Drop intermediate files (looped backgrounds, gradient fallbacks).
        # Deliverables are already removed above when R2 was used.
        _cleanup_local_intermediates(job_dir)

        # Robust env-var read: tolerate accidental whitespace or quotes that
        # Railway / .env files sometimes leave around the value. We default to
        # review-required so the safest behaviour applies if the var is missing.
        _require_review_raw = os.environ.get("REQUIRE_REVIEW")
        if _require_review_raw is None:
            _require_review = True
        else:
            _normalized = _require_review_raw.strip().strip('"').strip("'").lower()
            _require_review = _normalized in ("true", "1", "yes", "y", "on")
        final_status = "pending_review" if _require_review else "done"
        print(
            f"[PIPELINE] job={job_id} REQUIRE_REVIEW="
            f"{_require_review_raw!r} -> require_review={_require_review} "
            f"final_status={final_status}"
        )

        update_job(job_id, status=final_status, progress=100, files=files)

        # Send job completion email to the user (best-effort, never blocks render).
        try:
            import emails as _emails
            from database import SessionLocal as _SessionLocal, Job as _Job
            from database import User as _User, UserSettings as _UserSettings
            _ndb = _SessionLocal()
            try:
                _job_row = _ndb.query(_Job).filter(_Job.job_id == job_id).first()
                if _job_row and _job_row.user_id:
                    _usr = _ndb.query(_User).filter(_User.id == _job_row.user_id).first()
                    if _usr and _usr.email:
                        _settings = _ndb.query(_UserSettings).filter(
                            _UserSettings.user_id == _usr.id
                        ).first()
                        _prefs = (_settings.settings_json or {}) if _settings else {}
                        if _prefs.get("notif_jobs", False):
                            threading.Thread(
                                target=_emails.send_job_completed,
                                kwargs={
                                    "email": _usr.email,
                                    "username": _usr.username,
                                    "artist": artist or "",
                                    "filename": os.path.basename(mp3_path),
                                    "job_id": job_id,
                                },
                                daemon=True,
                            ).start()
            finally:
                _ndb.close()
        except Exception as _email_err:
            print(f"[PIPELINE] job completion email skipped: {_email_err}")

        # G4: pre-warm the ProRes deliverables in a background worker
        # job. When UMG eventually clicks "Master ProRes" the .mov is
        # already on R2 (302 instant) instead of paying 60-120 s of
        # ffmpeg in the request thread. Best-effort — never fail the
        # main render because the prewarm couldn't be enqueued.
        if wants_umg and final_status in ("done", "pending_review"):
            try:
                from queue_jobs import enqueue_prores_prewarm
                enqueue_prores_prewarm(job_id, "umg_master")
                enqueue_prores_prewarm(job_id, "umg_short")
            except Exception as e:  # pragma: no cover
                print(f"[PIPELINE] prores prewarm enqueue skipped: {e}")
    except Exception as exc:
        traceback.print_exc()
        update_job(job_id, status="error", error=str(exc))
        # Surface render failures to Sentry. The worker runs outside
        # the FastAPI request loop so the framework's auto-capture
        # doesn't fire — without this explicit hook, ffmpeg hangs,
        # OOMs, Veo 429-storms, etc. would be invisible. Wrapped to
        # never let observability break the failure path.
        try:
            import sentry_sdk
            with sentry_sdk.push_scope() as _scope:
                _scope.set_tag("event", "pipeline.failed")
                _scope.set_tag("job_id", job_id)
                _scope.set_tag("artist", artist or "?")
                sentry_sdk.capture_exception(exc)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Step 1 — Whisper transcription
# ---------------------------------------------------------------------------

# YouTube-uploader chatter we don't want in the lyrics. Tight-and-narrow:
# every entry must be a multi-word phrase or unambiguous YouTuber jargon
# that essentially never shows up in song lyrics. The previous broader
# list killed legit content on UMG videos that open with dialogue/intros
# (Karol G "Si Antes Te Hubiera Conocido (Official Video)" had
# "¡Gracias! ¡Qué linda! ¡Gracias!" filtered as spam — that's the artist
# thanking the audience in the video, not channel chatter, and the
# operator wanted it transcribed).
_SPAM_PATTERNS = [
    "suscribete al canal", "subscribe to my channel",
    "thanks for watching", "thanks for listening",
    "link in description", "link in the description",
    "link en la descripcion", "link en descripcion",
    "all rights reserved", "todos los derechos reservados",
    "escucha en spotify", "available on spotify",
    "apple music", "deezer", "amazon music",
    "music by", "produced by", "lyrics by",
]


# Whisper-1 has a small set of "training-data hallucinations" that fire
# during silence / quiet music intros — phrases lifted directly from
# subtitle datasets (Amara.org credits, "♪ music ♪" tags) that the
# model emits as a sequence completion when there's no real speech.
# These are NOT YouTube uploader chatter (above) — they're outputs that
# come straight from training data leakage. Tight match because we want
# zero false positives on real lyrics.
_WHISPER_HALLUCINATIONS = [
    "subtitulos realizados por la comunidad de amara.org",
    "subtitled by the amara.org community",
    "subtitles by the amara.org community",
    "subtitling by the amara.org community",
    "transcribed by amara",
    "amara.org",  # short form catches "Visit amara.org"
    "subtitles created by",
    "subtitles by:",
    "subtitulado por",
    "transcripcion por",
    "♪ music ♪",
    "[ music ]",
    "[music]",
]


def _is_whisper_hallucination(text: str) -> bool:
    """True if the segment text matches a known Whisper training-data
    hallucination. Match is case- and accent-insensitive but does NOT
    use loose substring matching — we compare a normalized string."""
    if not text:
        return False
    s = _normalize_token(text) if "_normalize_token" in globals() else text.lower().strip()
    # Direct lower-ASCII compare for the denylist (defensive: we don't
    # rely on _normalize_token having been hoisted yet at module load).
    import unicodedata as _u
    s = _u.normalize("NFD", text or "").encode("ascii", "ignore").decode("ascii").lower().strip()
    s = " ".join(s.split())  # collapse whitespace
    for needle in _WHISPER_HALLUCINATIONS:
        if needle in s:
            return True
    return False


def _is_single_word_loop(text: str, min_repeats: int = 8) -> bool:
    """True if `text` is essentially the same short word repeated many
    times — Whisper-1's classic outro/sustained-vocal failure mode
    ("oh, oh, oh, oh, …" × 100). We detect by checking that, after
    normalising, ≥ 90 % of tokens are the same single word AND the
    repeat count is ≥ `min_repeats`.

    This catches the AIRBAG / River Plate case (110 "oh"s in a 30 s
    segment) without flagging real lyrics: a chorus like "oh-oh-oh I
    love you oh-oh" stays mixed enough that the dominant token never
    reaches 90 % concentration.
    """
    if not text:
        return False
    tokens = [n for n in (_normalize_token(w) for w in text.split()) if n]
    if len(tokens) < min_repeats:
        return False
    from collections import Counter as _C
    counts = _C(tokens)
    top_token, top_count = counts.most_common(1)[0]
    # Require the dominant token to be short (≤ 4 chars) so we don't
    # collapse a verse that legitimately repeats a longer word.
    if len(top_token) > 4:
        return False
    return top_count / len(tokens) >= 0.9 and top_count >= min_repeats


def _filter_whisper_hallucinations(segments: list[dict]) -> tuple[list[dict], int]:
    """Drop segments whose text is a known Whisper hallucination phrase
    OR a single-word loop (e.g. "oh, oh, oh, …" outro fills). The
    single-word filter runs BEFORE _detect_hallucination in the caller
    so a legitimate transcription with a loopy outro doesn't get its
    whole timeline thrown out by the recovery branch.

    Returns (filtered_segments, dropped_count) for logging.
    """
    if not segments:
        return segments, 0
    out = []
    for s in segments:
        text = s.get("text") or ""
        if _is_whisper_hallucination(text):
            continue
        if _is_single_word_loop(text):
            print(f"[WHISPER] dropping single-word loop "
                  f"({(float(s.get('end',0)) - float(s.get('start',0))):.1f}s): "
                  f"{text[:60]!r}")
            continue
        out.append(s)
    return out, len(segments) - len(out)


def _filter_intro_song_overlap(
    intro_segs: list[dict],
    song_segs: list[dict],
    threshold: float = 0.7,
) -> tuple[list[dict], int]:
    """Drop intro Whisper segments that fuzzy-match the song's opening
    lines. When a user uploads a track with an instrumental intro,
    Whisper run on the prefix slice often hallucinates the first verse
    (using the lrclib `lyrics_hint` as a noisy prior) at start≈0. We
    then concatenate that hallucinated segment in front of the
    LRCLIB-aligned song segments — producing a phantom "first line at
    0:00.0" in the editor while the real first line sits at the offset.

    Heuristic: only drop intro segs whose start is before the song's
    first sung line AND whose normalised text fuzzy-matches one of the
    first few song segments (≥ `threshold`). Intro segs that sit fully
    inside the instrumental window but transcribe genuinely different
    text (e.g. a spoken-word preamble) survive.
    """
    if not intro_segs or not song_segs:
        return intro_segs, 0
    from difflib import SequenceMatcher

    def _norm(t: str) -> str:
        return _normalize_token(" ".join((t or "").split()))

    song_heads = [_norm(s.get("text") or "") for s in song_segs[:4]]
    song_first_start = float(song_segs[0].get("start", 0.0))
    kept: list[dict] = []
    dropped = 0
    for s in intro_segs:
        t_norm = _norm(s.get("text") or "")
        if not t_norm:
            kept.append(s)
            continue
        if float(s.get("start", 0.0)) >= song_first_start:
            kept.append(s)
            continue
        is_dup = any(
            sh and SequenceMatcher(None, t_norm, sh).ratio() >= threshold
            for sh in song_heads
        )
        if is_dup:
            dropped += 1
            continue
        kept.append(s)
    return kept, dropped


_WHISPER_MODELS: dict = {}
_WHISPER_LOCK = None


def _fix_lrc_first_line_at_zero(
    segments: list[dict],
    audio_duration: float | None = None,
) -> tuple[list[dict], float | None]:
    """Auto-correct the lrclib "first line at [00:00.00]" quirk.

    A non-trivial fraction of community-curated LRCs anchor line 1 to
    song time 0 even when there's a long instrumental intro before the
    first vocal. Trusting the LRC then shows the first lyric pinned to
    0:00 in the editor / video while the actual vocal entry sits ~15 s
    later — exactly the bug the operator hit on Intoxicados — "No Tengo
    Ganas".

    We can't ground-truth the vocal entry without a VAD pass, but the
    LRC's OWN cadence betrays the quirk: the gap between line 1 and
    line 2 is dramatically larger than the median gap between
    subsequent verse / chorus lines. When all three of these hold we
    relocate line 1 to ``line2.start - median_gap`` (the spot a normal
    cadence would put it):

      - segments[0].start <= 1.0
      - gap(line1, line2) > 2 × median(gaps in lines 2..6)
      - gap(line1, line2) > 8 s in absolute terms

    The thresholds are conservative — a song with a normal 4-second
    intro on line 1 won't false-positive (gap to line 2 is ~8 s but
    the ratio against the median typically isn't > 2×).

    Returns (segments_with_first_fixed, suggested_new_start_or_None).
    The second value is logged by the caller for observability.
    """
    if len(segments) < 4:
        return segments, None
    first = segments[0]
    second = segments[1]
    first_start = float(first.get("start", 0.0))
    second_start = float(second.get("start", 0.0))
    if first_start > 1.0:
        return segments, None
    gaps: list[float] = []
    for i in range(1, min(len(segments) - 1, 6)):
        gaps.append(float(segments[i + 1]["start"]) - float(segments[i]["start"]))
    if not gaps:
        return segments, None
    gaps.sort()
    median_gap = gaps[len(gaps) // 2]
    first_gap = second_start - first_start
    if median_gap <= 0:
        return segments, None
    if first_gap < median_gap * 2 or first_gap < 8.0:
        return segments, None
    suggested = max(0.0, second_start - median_gap)
    seg_dur = max(0.5, float(first.get("end", suggested)) - first_start)
    new_end = suggested + seg_dur
    if audio_duration:
        new_end = min(float(audio_duration), new_end)
    # Don't let the new line bleed into line 2.
    if new_end > second_start - 0.05:
        new_end = max(suggested + 0.5, second_start - 0.05)
    fixed_first = {**first, "start": suggested, "end": new_end}
    return [fixed_first] + list(segments[1:]), suggested


def _get_whisper_model(name: str = "turbo"):
    """Load a Whisper model once per process and reuse. Thread-safe. Supports
    multiple sizes cached side-by-side so we can fall back turbo -> large-v3
    without re-loading the first one."""
    global _WHISPER_LOCK
    import whisper
    import threading as _t
    if _WHISPER_LOCK is None:
        _WHISPER_LOCK = _t.Lock()
    with _WHISPER_LOCK:
        if name not in _WHISPER_MODELS:
            print(f"[WHISPER] Loading model '{name}' (one-time)")
            _WHISPER_MODELS[name] = whisper.load_model(name)
    return _WHISPER_MODELS[name]


_WHISPER_API_MAX_BYTES = 24 * 1024 * 1024  # 25 MB ceiling, with 1 MB headroom


def _compress_for_whisper(input_path: str) -> str:
    """If `input_path` exceeds Whisper-API's 25 MB ceiling (typical for
    UMG-style WAV uploads), produce a temp 128 kbps mono MP3 alongside
    it and return the new path. Otherwise return the original path
    unchanged. Caller is responsible for cleaning up the temp file when
    `_compress_for_whisper(p) != p`.

    Why mono 128 kbps: Whisper's transcription accuracy is bounded well
    above this bitrate — extra fidelity doesn't help. Stereo→mono cuts
    size in half with zero impact on Whisper. A 4-min track lands around
    4 MB, comfortably under the cap.
    """
    try:
        sz = os.path.getsize(input_path)
    except OSError:
        return input_path
    if sz <= _WHISPER_API_MAX_BYTES:
        return input_path
    import subprocess as _sp
    out = input_path + ".whisper.mp3"
    try:
        _sp.run(
            ["ffmpeg", "-y", "-i", input_path,
             "-ac", "1", "-b:a", "128k", "-loglevel", "error", out],
            check=True, timeout=120,
            capture_output=True, text=True,
        )
    except (_sp.CalledProcessError, _sp.TimeoutExpired,
            FileNotFoundError, OSError) as e:
        # Previously this swallowed the error and returned the original
        # 30-50 MB file. The Whisper API would then 413 / 400 and the
        # operator saw a generic "Error al procesar" with no diagnostic.
        # Surface the real cause via the pipeline catch-all (which sets
        # job.error and tags Sentry) instead.
        stderr = (getattr(e, "stderr", "") or "") if isinstance(
            e, _sp.CalledProcessError
        ) else ""
        raise RuntimeError(
            f"audio_compression_failed: ffmpeg no pudo transcodificar "
            f"{os.path.basename(input_path)} para Whisper API "
            f"(tamaño {sz/1e6:.1f} MB > {_WHISPER_API_MAX_BYTES/1e6:.0f} MB). "
            f"Detalle: {(stderr or str(e))[-500:]}"
        ) from e
    if not os.path.exists(out) or os.path.getsize(out) == 0:
        raise RuntimeError(
            f"audio_compression_failed: ffmpeg returned 0 but produced "
            f"an empty/missing output at {out!r}"
        )
    new_sz = os.path.getsize(out)
    print(f"[WHISPER-API] compressed {sz/1e6:.1f} MB → "
          f"{new_sz/1e6:.1f} MB for API limit")
    return out


def _transcribe_via_openai_api(mp3_path: str, language: str | None = None,
                                lyrics_hint: str | None = None,
                                job_id: str | None = None) -> list[dict]:
    """Transcribe by calling OpenAI's Whisper API. Returns the same segments
    structure as the local Whisper path. Used in production where loading
    the local model would consume too much worker RAM (~3 GB) and risks OOM.

    Cost: ~$0.006 per minute of audio (~$0.02 per song).

    `lyrics_hint`: if provided, the FIRST ~200 tokens of this string are
    used as Whisper's `prompt` parameter — orienting the model's
    vocabulary toward the actual lyrics it should expect. This is the
    documented Whisper-API mechanism for biasing transcription
    (https://platform.openai.com/docs/guides/speech-to-text/prompting).
    Significantly reduces hallucination loops on tracks where Whisper
    otherwise drifts (e.g. confusing artist-name ad-libs for the lyric
    line). Only the last 224 tokens are read by Whisper and only the
    first ~30 s of audio benefits from it; on longer tracks the help is
    most impactful at the song's start where the model establishes its
    interpretation.

    Why whisper-1 and not gpt-4o-transcribe (better text quality):
        gpt-4o-transcribe and gpt-4o-mini-transcribe only return plain
        text — no segment timestamps. This pipeline renders lyrics
        synchronized to the audio, so segment-level start/end times are
        non-negotiable. whisper-1 (whisper-large-v2) is the only OpenAI
        transcription model that returns verbose_json with segment
        timestamps as of 2026-04.
    """
    from openai import OpenAI

    client = OpenAI()  # picks up OPENAI_API_KEY from env
    print(f"[WHISPER-API] transcribing {os.path.basename(mp3_path)} via OpenAI (whisper-1)")

    # Build the initial prompt. When the caller has reference lyrics
    # (typically from lrclib plain), we ship the first ~200 tokens of
    # them so Whisper expects that vocabulary. Otherwise fall back to a
    # generic "song lyrics" hint.
    if lyrics_hint and lyrics_hint.strip():
        # ~200 tokens ≈ 800 chars for Spanish/English. Whisper truncates
        # silently if longer; this just keeps logs cleaner.
        prompt_text = lyrics_hint.strip()[:800]
        print(f"[WHISPER-API] initial_prompt primed with "
              f"{len(prompt_text)} chars from reference lyrics")
    else:
        prompt_text = ("Letras de canción:" if (language or "").startswith("es")
                       else "Song lyrics:")

    kwargs = {
        "model": "whisper-1",
        "response_format": "verbose_json",
        "timestamp_granularities": ["segment"],
        "prompt": prompt_text,
        # temperature=0 gives the most confident output; we lower the
        # default 0.0 ladder so it doesn't sample alternative
        # interpretations on tricky words.
        "temperature": 0.0,
    }
    if language:
        kwargs["language"] = language

    # Whisper-API rejects > 25 MB. UMG uploads lossless WAV (often 30-50
    # MB for a 3-min track). Transcode-compress only when over the cap;
    # the compressed copy is just for the API call — original audio is
    # untouched and used by the rest of the render pipeline.
    api_path = _compress_for_whisper(mp3_path)
    cleanup_compressed = api_path != mp3_path
    # Provenance record for the cost dashboard. job_id is optional — paths
    # that call transcribe() without a job_id (one-off scripts, tests)
    # skip the recording, and the cost panel just under-reports those
    # outliers. The OpenAI call itself is the same either way.
    recorder = None
    if job_id:
        from provenance import record_ai_call
        recorder = record_ai_call(
            job_id=job_id,
            step="whisper_transcribe",
            tool_name="whisper-1",
            tool_provider="openai",
            prompt=prompt_text[:500],
            input_data_types=["audio_file"],
        )
    # Retry loop with exponential backoff + jitter for transient failures
    # (rate-limits, connection drops). Before this loop, a single 429 from
    # OpenAI bubbled straight to the user as 503 with no retry — the message
    # claimed "Reintentamos en unos segundos" but actually didn't.
    # Incident 2026-05-14: Agus + admin transcribiendo en paralelo →
    # cascade 503. Now: 5 attempts over ~30s before surrendering.
    from fastapi import HTTPException
    try:
        from openai import RateLimitError, APIConnectionError, APIError
    except ImportError:
        RateLimitError = APIConnectionError = APIError = ()

    import random
    import time as _time_retry
    _MAX_RETRIES = int(os.environ.get("WHISPER_MAX_RETRIES", "5"))
    response = None
    last_exc = None
    try:
        for attempt in range(_MAX_RETRIES):
            try:
                with open(api_path, "rb") as f:
                    kwargs["file"] = f
                    response = client.audio.transcriptions.create(**kwargs)
                if attempt > 0:
                    print(f"[WHISPER-API] succeeded on attempt {attempt + 1}/{_MAX_RETRIES}")
                break
            except Exception as exc:
                last_exc = exc
                # Retryable transients: rate-limit + connection drops.
                if isinstance(exc, (RateLimitError, APIConnectionError)):
                    if attempt < _MAX_RETRIES - 1:
                        # 2^attempt + jitter: 1-2s, 2-3s, 4-5s, 8-9s, 16-17s
                        sleep_s = (2 ** attempt) + random.uniform(0, 1)
                        kind = "rate-limit" if isinstance(exc, RateLimitError) else "connection"
                        print(
                            f"[WHISPER-API] transient {kind} error on attempt "
                            f"{attempt + 1}/{_MAX_RETRIES}: {exc!s}; "
                            f"sleeping {sleep_s:.1f}s then retrying"
                        )
                        _time_retry.sleep(sleep_s)
                        continue
                    # Last attempt failed — fall through to raise below.
                # Non-retryable (APIError, OSError, etc.) — bail immediately.
                break
        else:
            # for/else: only reached if loop completed without break (shouldn't happen)
            pass

        if response is None:
            # Translate the final exception to HTTPException for the UI.
            if isinstance(last_exc, RateLimitError):
                raise HTTPException(
                    status_code=503,
                    detail=(
                        f"Servicio de transcripción saturado tras {_MAX_RETRIES} reintentos. "
                        "Reintentá en un minuto."
                    ),
                    headers={"Retry-After": "60"},
                ) from last_exc
            if isinstance(last_exc, APIConnectionError):
                raise HTTPException(
                    status_code=502,
                    detail="No pudimos contactar el servicio de transcripción. Reintentá en unos segundos.",
                ) from last_exc
            if isinstance(last_exc, APIError):
                raise HTTPException(
                    status_code=502,
                    detail=f"Servicio de transcripción no disponible: {last_exc!s}",
                ) from last_exc
            # Unknown exception type — re-raise the original.
            if last_exc is not None:
                raise last_exc
    finally:
        if cleanup_compressed:
            try:
                os.unlink(api_path)
            except OSError:
                pass

    if recorder is not None:
        # Mark the provenance row finished so the dashboard counts this
        # call and the reaper does not mistake it for an in-flight orphan.
        try:
            recorder.finish(response_summary=f"whisper_transcribe_ok")
        except Exception:
            pass

    raw_segments = response.segments or []
    import re as _re

    segments: list[dict] = []
    for seg in raw_segments:
        text = (seg.text or "").strip()
        if not text or len(text) < 3:
            continue
        # Same filters as local path so behavior matches.
        if _re.search(r'[一-鿿぀-ゟ゠-ヿ가-힯]', text):
            print(f"[WHISPER-API] Filtered non-latin: {text[:60]}")
            continue
        if any(spam in text.lower() for spam in _SPAM_PATTERNS):
            print(f"[WHISPER-API] Filtered spam: {text[:60]}")
            continue
        # Only drop segments that Whisper is VERY sure aren't speech. The
        # previous 0.7 threshold was tossing legitimate lyric lines on
        # tracks with dense crowd noise / heavy mix (Karol G's "Yo Me
        # Caso Contigo" interlude, audience cheering on live cuts). The
        # operator can prune obvious non-lyrics in the editor; better to
        # surface borderline content than to silently drop it.
        if (seg.no_speech_prob or 0) > 0.92:
            print(f"[WHISPER-API] Filtered very-low-confidence: {text[:60]}")
            continue
        segments.append({
            "start": float(seg.start),
            "end": float(seg.end),
            "text": text,
        })

    # Whisper hallucinates loops in two distinct shapes:
    #   1. SAME LINE REPEATED across consecutive segments — easy: dedupe
    #      by exact match, keep first 2 (preserves legit chorus repeats).
    #   2. SAME PHRASE REPEATED INSIDE a single segment's text — happens
    #      on long sustained / instrumental passages where Whisper emits
    #      one large segment whose text is "X and X and X and X and …".
    #      Need to detect intra-segment repetition and truncate.
    #
    # Both cases observed in production on "El Plan de la Mariposa - El
    # Riesgo" (5/5/2026): segment 60-150s contained the line "que podía
    # reflexionar sobre lo que estaba haciendo y" repeated ~5 times within
    # the same segment.
    import re as _re_loops

    def _truncate_intra_loop(text: str) -> tuple[str, float]:
        """If text contains a phrase that repeats 3+ times consecutively,
        truncate to the first 2 occurrences. Phrase = 4–14 word window —
        the upper bound matters because Whisper hallucinations sometimes
        loop on a clause that's 8–12 words long.

        Returns (truncated_text, ratio_kept) so the caller can shrink the
        segment's end timestamp proportionally — without that adjustment,
        the truncated text would stay on screen during the instrumental
        passage Whisper was hallucinating over, giving a "stuck subtitle"
        feel. Ratio = 1.0 when nothing changes.
        """
        words = text.split()
        total = len(words)
        if total < 12:
            return text, 1.0
        for window in range(14, 3, -1):  # try longer windows first
            if total < window * 3:
                continue
            for start in range(total - window * 3 + 1):
                phrase = words[start:start + window]
                count = 1
                pos = start + window
                while pos + window <= total and words[pos:pos + window] == phrase:
                    count += 1
                    pos += window
                if count >= 3:
                    cut = start + window * 2
                    truncated = " ".join(words[:cut])
                    truncated = truncated.rstrip(",.;: ") + "…"
                    ratio = cut / total
                    return truncated, ratio
        return text, 1.0

    cleaned: list[dict] = []
    intra_truncated = 0
    for seg in segments:
        original = seg["text"]
        new_text, ratio = _truncate_intra_loop(original)
        if new_text != original:
            intra_truncated += 1
            duration = seg["end"] - seg["start"]
            seg = {
                **seg,
                "text": new_text,
                # Shrink end so the subtitle leaves the screen when the
                # legitimate spoken phrase ends, not when Whisper's
                # hallucination tail would have ended.
                "end": seg["start"] + duration * ratio,
            }
        cleaned.append(seg)
    if intra_truncated:
        print(f"[WHISPER-API] Truncated intra-segment loops in {intra_truncated} segment(s)")
    segments = cleaned

    # Collapse consecutive-identical-text segments into a single segment
    # spanning the whole streak. This handles two cases the same way:
    #
    #  - Whisper hallucination loop ("¡Karol!" 174 times): the original
    #    code DROPPED segments past the 2nd, leaving a 17 s hole in the
    #    video. The chant audio is still in the audio track, but the
    #    subtitle disappears mid-chant — looks broken.
    #  - Real audience chant or repeated ad-lib in a live cut (Karol G
    #    "Si Antes Te Hubiera Conocido (Official Video)" has the audience
    #    chanting "¡Karol!" for ~17 s during the bridge): same shape, but
    #    here the chant IS legit content the operator may want to keep.
    #
    # Either way: the right output is a single subtitle covering the
    # whole chant, not 174 micro-segments and not silence. The operator
    # decides in the editor whether to keep or drop.
    merged: list[dict] = []
    collapsed_groups = 0
    collapsed_total = 0
    for seg in segments:
        key = seg["text"].lower().strip()
        if merged and merged[-1]["text"].lower().strip() == key:
            # Extend the previous segment's end to cover this duplicate.
            if seg["end"] > merged[-1]["end"]:
                merged[-1]["end"] = seg["end"]
            collapsed_total += 1
            # First merge in a streak counts as a new collapsed group.
            if not merged[-1].get("_collapsed"):
                merged[-1]["_collapsed"] = True
                collapsed_groups += 1
        else:
            merged.append({**seg})
    for s in merged:
        s.pop("_collapsed", None)
    if collapsed_total:
        print(f"[WHISPER-API] Merged {collapsed_total} consecutive duplicate "
              f"segments into {collapsed_groups} chant/loop spans")
    segments = merged

    GAP = 0.05
    for i in range(len(segments) - 1):
        if segments[i]["end"] > segments[i + 1]["start"] - GAP:
            segments[i]["end"] = segments[i + 1]["start"] - GAP

    # Drop training-data-leak phrases AND single-word "oh oh oh"
    # outro loops BEFORE returning, so the caller's hallucination
    # detector sees a clean timeline. Without this, a 100-"oh"
    # outro would trip the "implausible mega-segment" detector and
    # the caller would discard the entire (otherwise good) Whisper
    # output, replacing it with reference lyrics distributed at
    # synthetic timestamps — losing the accurate timing of the
    # legitimate verses.
    segments, _dropped_loops = _filter_whisper_hallucinations(segments)
    if _dropped_loops:
        print(f"[WHISPER-API] filtered {_dropped_loops} hallucination/loop segment(s)")

    print(f"[WHISPER-API] {len(segments)} segments")
    return segments


def transcribe(mp3_path: str, language: str = None,
               lyrics_hint: str | None = None,
               job_id: str | None = None) -> list[dict]:
    """Transcribe an audio file to lyric segments.

    Backend selection:
        - If OPENAI_API_KEY is set, route to the OpenAI Whisper API. This is
          the production path: no local model, no OOM risk on 1-2 GB workers.
          Errors propagate — no silent fallback to the 1.5 GB local model
          that frequently OOMs on small instances.
        - If OPENAI_API_KEY is not set, fall back to the local Whisper-turbo
          model. Works for development on machines with enough RAM.

    `lyrics_hint`: optional reference text (e.g. lrclib plain lyrics) used
    as Whisper-API's `prompt` parameter to bias transcription toward the
    expected vocabulary. See _transcribe_via_openai_api for details. Local
    Whisper path ignores it (could be added later via `initial_prompt=`).
    """
    has_key = bool(os.environ.get("OPENAI_API_KEY", "").strip())
    print(f"[transcribe] OPENAI_API_KEY={'set' if has_key else 'EMPTY'}")
    if has_key:
        return _transcribe_via_openai_api(
            mp3_path, language=language, lyrics_hint=lyrics_hint,
            job_id=job_id,
        )

    # --- local Whisper path ---
    audio_path = mp3_path

    model = _get_whisper_model("turbo")

    kwargs = dict(
        word_timestamps=True,
        initial_prompt="Lyrics:",
        condition_on_previous_text=False,
    )
    if language:
        kwargs["language"] = language
        print(f"[WHISPER] Forced language: {language}")

    result = model.transcribe(audio_path, **kwargs)

    import re as _re

    segments = []
    for seg in result["segments"]:
        text = seg["text"].strip()
        if not text or len(text) < 3:
            continue
        # Filter non-latin characters (Demucs artifacts like "Lil怎麼樣")
        if _re.search(r'[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff\uac00-\ud7af]', text):
            print(f"[WHISPER] Filtered non-latin artifact: {text[:60]}")
            continue
        # Filter spam/non-lyrics
        if any(spam in text.lower() for spam in _SPAM_PATTERNS):
            print(f"[WHISPER] Filtered spam: {text[:60]}")
            continue
        # Filter high no_speech_prob segments (likely hallucinations)
        if seg.get("no_speech_prob", 0) > 0.7:
            print(f"[WHISPER] Filtered low-confidence (no_speech={seg['no_speech_prob']:.2f}): {text[:60]}")
            continue
        words = seg.get("words", [])
        if words:
            start = words[0]["start"]
            end = words[-1]["end"]
        else:
            start = seg["start"]
            end = seg["end"]
        segments.append({"start": start, "end": end, "text": text})

    # Safety net: retry if first segment starts very late
    if segments and segments[0]["start"] > 30:
        print(f"[WHISPER] WARNING: first seg at {segments[0]['start']:.1f}s, retrying")
        kwargs2 = dict(kwargs, initial_prompt="Song lyrics transcription:", no_speech_threshold=0.4)
        result2 = model.transcribe(mp3_path, **kwargs2)
        segments2 = []
        for seg in result2["segments"]:
            text = seg["text"].strip()
            if not text or len(text) < 3:
                continue
            words = seg.get("words", [])
            if words:
                segments2.append({"start": words[0]["start"], "end": words[-1]["end"], "text": text})
            else:
                segments2.append({"start": seg["start"], "end": seg["end"], "text": text})
        if segments2 and segments2[0]["start"] < segments[0]["start"]:
            segments = segments2

    # Quality fallback: if turbo produced a sparse/low-confidence result, retry
    # with large-v3 (slower but much more accurate, especially for noisy vocals
    # or heavy accents). Gated by WHISPER_FALLBACK_ENABLED to save RAM on small
    # machines that cannot hold both models at once.
    if os.environ.get("WHISPER_FALLBACK_ENABLED", "1") != "0":
        if len(segments) < 5:
            print(f"[WHISPER] Only {len(segments)} segments with turbo; "
                  f"falling back to large-v3")
            try:
                large = _get_whisper_model("large-v3")
                result3 = large.transcribe(audio_path, **kwargs)
                segments3 = []
                for seg in result3["segments"]:
                    text = seg["text"].strip()
                    if not text or len(text) < 3:
                        continue
                    if any(spam in text.lower() for spam in _SPAM_PATTERNS):
                        continue
                    words = seg.get("words", [])
                    if words:
                        segments3.append({"start": words[0]["start"],
                                          "end": words[-1]["end"], "text": text})
                    else:
                        segments3.append({"start": seg["start"],
                                          "end": seg["end"], "text": text})
                if len(segments3) > len(segments):
                    print(f"[WHISPER] large-v3 produced {len(segments3)} "
                          f"segments (turbo: {len(segments)}); using large-v3")
                    segments = segments3
            except Exception as e:
                print(f"[WHISPER] large-v3 fallback failed: {e}; keeping turbo")

    for i, seg in enumerate(segments[:5]):
        print(f"[WHISPER] seg {i}: {seg['start']:.2f}–{seg['end']:.2f}  {seg['text'][:60]}")

    GAP = 0.05
    for i in range(len(segments) - 1):
        if segments[i]["end"] > segments[i + 1]["start"] - GAP:
            segments[i]["end"] = segments[i + 1]["start"] - GAP

    # Same outro-loop filter as the API path — see notes there.
    segments, _dropped_loops = _filter_whisper_hallucinations(segments)
    if _dropped_loops:
        print(f"[WHISPER] filtered {_dropped_loops} hallucination/loop segment(s)")

    return segments


# ---------------------------------------------------------------------------
# Lyrics reference fetcher — used by /transcribe to show reference text in UI.
#
# The reference is fed to LyricsEditor.findSuggestion which fuzzy-matches each
# Whisper segment to a reference line and surfaces a one-click correction.
# Quality of suggestions is bounded by quality of the reference, so we lean
# on Gemini 2.5 Flash with the google_search grounding tool — Google's
# grounded LLM aggregates from public lyric sites with cleaner provenance
# than direct API integration with any single commercial lyrics provider
# (Genius prohibits commercial use without a license; UMG cannot ride that).
# ---------------------------------------------------------------------------

# Domains we *prefer* in grounding sources. Soft signal only — Vertex AI
# Search wraps grounding URIs in a redirect host (vertexaisearch.cloud
# .google.com/...) so the original target host is often hidden until the
# redirect is followed. We log it for observability but do NOT reject on
# absence; the lyrics-shape validation downstream handles hallucination.
_LYRIC_DOMAINS = {
    "genius.com", "azlyrics.com", "letras.com", "letras.mus.br",
    "lyrics.com", "musixmatch.com", "songlyrics.com", "metrolyrics.com",
    "lyricfind.com", "songmeanings.com",
}


def _truthy_env(val: str) -> bool:
    """Robust truthy parser — same shape as REQUIRE_REVIEW (commit 06a42e7)."""
    return (val or "").strip().strip('"').strip("'").lower() in (
        "1", "true", "yes", "on", "y", "t",
    )


def _lyrics_cache_key(artist: str, song: str) -> str:
    import hashlib
    return hashlib.sha1(
        f"{artist.lower().strip()}|{song.lower().strip()}".encode()
    ).hexdigest()[:16]


def _lrclib_cache_key(artist: str, song: str) -> str:
    """Stable namespaced key for lrclib results in the LyricsCache table.
    Same Postgres table the Gemini path uses; the `lrclib:` prefix keeps
    the two namespaces independent (Gemini stores plain text in `lyrics`,
    lrclib stores a JSON-encoded dict)."""
    import hashlib as _h
    h = _h.sha1(f"{artist.strip().lower()}|{song.strip().lower()}".encode())
    return f"lrclib:{h.hexdigest()[:16]}"


def _fetch_lrclib(artist: str, song: str, db=None) -> dict | None:
    """Look up a song on lrclib.net's public API. Returns:
        {"plain": str|None, "synced": str|None, "duration": float|None}
    or None if the request failed or the song wasn't found.

    lrclib.net is an open, free, no-auth lyrics database (similar shape to
    MusicBrainz for lyrics). Public API, no anti-bot, generous rate limits.
    Crucially: covers Latin / reggaeton / pop catalogues that Gemini-grounded
    search refuses to answer for due to RECITATION blocking on UMG-owned
    songs (Karol G, Bad Bunny, J Balvin, etc.). It also frequently has
    *synced* lyrics with line-level timestamps — when present, those let us
    skip Whisper transcription entirely and avoid hallucination loops.

    `db` (optional): a SQLAlchemy session. When provided, the function
    consults the LyricsCache table first and writes successful fetches
    back so that future calls don't depend on lrclib.net's uptime. This
    is what saves us when Railway's outbound to lrclib gets a transient
    timeout — once we've fetched a song once, we never re-fetch it.

    Best-effort — never raises.
    """
    if not artist or not song:
        return None
    import requests as _req
    import json as _json

    cache_key = _lrclib_cache_key(artist, song)

    # Cache lookup. Skip entirely on db=None (e.g. the smoke scripts).
    if db is not None:
        try:
            from database import LyricsCache
            row = db.query(LyricsCache).filter(
                LyricsCache.cache_key == cache_key
            ).first()
            if row and row.lyrics:
                cached = _json.loads(row.lyrics)
                if cached.get("plain") or cached.get("synced"):
                    print(f"[LYRICS] lrclib cache hit {cache_key} "
                          f"({len((cached.get('plain') or ''))} plain chars, "
                          f"synced={'yes' if cached.get('synced') else 'no'})")
                    return cached
        except Exception as e:
            print(f"[LYRICS] lrclib cache read failed: {e}")
    # Two attempts: lrclib reads can spike >10s under load. Total budget
    # is ~25s in the worst case, well within the user-perceived bound
    # for /transcribe (Whisper is the long pole anyway).
    last_err: Exception | None = None
    r = None
    for attempt in range(2):
        try:
            r = _req.get(
                "https://lrclib.net/api/get",
                params={"artist_name": artist, "track_name": song},
                timeout=20,
                headers={"User-Agent": "GenLyAI/1.0 (+https://app.genly.pro)"},
            )
            break
        except Exception as e:  # transient network / timeout
            last_err = e
            if attempt == 0:
                print(f"[LYRICS] lrclib attempt 1 failed ({e.__class__.__name__}: "
                      f"{str(e)[:80]}); retrying once")
                continue
            print(f"[LYRICS] lrclib fetch failed after retry: {e}")
            return None
    if r is None:
        result = None
    elif r.status_code != 200:
        print(f"[LYRICS] lrclib /get {r.status_code} for {artist!r} - {song!r}")
        result = None
    else:
        try:
            result = _parse_lrclib_record(r.json())
        except Exception as e:
            print(f"[LYRICS] lrclib /get parse failed: {e}")
            result = None

    # Fallback: si /api/get no devolvió un record útil (404, transient,
    # null fields), intentar /api/search. Es fuzzy: busca con keywords
    # combinados y devuelve hasta N candidates. Pickeamos el que mejor
    # matchee artist+song con preferencia para syncedLyrics. Caso real
    # motivador: Noches Sin Sueño (Rata Blanca) — /api/get devolvió 404
    # transient en staging, /api/search habría devuelto 4 candidates
    # válidos con synced perfecto, evitando el bug del Gemini fallback.
    if result is None:
        candidates = _try_lrclib_search(artist, song)
        if candidates:
            best = _pick_best_lrclib_candidate(candidates, artist, song)
            if best is not None:
                print(f"[LYRICS] lrclib /get failed but /search rescued "
                      f"candidate id={best.get('id')} "
                      f"({best.get('artistName')!r} - {best.get('trackName')!r}, "
                      f"synced={'yes' if best.get('syncedLyrics') else 'no'})")
                try:
                    result = _parse_lrclib_record(best)
                except Exception as e:
                    print(f"[LYRICS] lrclib /search parse failed: {e}")
                    result = None

    if result is None:
        return None

    # Write-through cache. Once stored, this song never depends on
    # lrclib.net uptime again — important for Railway outbound flakes.
    if db is not None:
        try:
            from database import LyricsCache
            payload = _json.dumps(result, ensure_ascii=False)
            row = db.query(LyricsCache).filter(
                LyricsCache.cache_key == cache_key
            ).first()
            if row:
                row.lyrics = payload
            else:
                db.add(LyricsCache(
                    cache_key=cache_key,
                    artist=artist,
                    title=song,
                    lyrics=payload,
                    fetched_by_model="lrclib",
                ))
            db.commit()
            print(f"[LYRICS] lrclib cached {cache_key} "
                  f"({len(payload)} bytes)")
        except Exception as e:
            print(f"[LYRICS] lrclib cache write failed: {e}")
    return result


def _parse_lrclib_record(data: dict) -> dict | None:
    """Parsea un dict crudo de lrclib (de /api/get o de un item de
    /api/search) al shape `{plain, synced, duration}` que usa el rest
    del pipeline. Devuelve None si el record no tiene ni plain ni synced.

    Extraído del cuerpo de `_fetch_lrclib` para que el fallback a
    /api/search pueda reusar la misma lógica (incluido el derive de
    plain desde synced cuando lrclib solo expone synced).
    """
    plain = (data.get("plainLyrics") or "").strip() or None
    synced = (data.get("syncedLyrics") or "").strip() or None
    if not plain and not synced:
        return None
    # Some lrclib records expose only `syncedLyrics` (different bots
    # populate the two columns independently). The downstream auto-
    # recover code in /transcribe gates on `if plain:` so when plain
    # is missing the recovery branch is unreachable. Derive plain from
    # synced by stripping the `[mm:ss.xx]` timestamps so the recovery
    # path always has a usable reference.
    if not plain and synced:
        import re as _re
        ts_re = _re.compile(r"^\s*(?:\[\d+:\d+(?:[.:]\d+)?\]\s*)+")
        derived: list[str] = []
        for line in synced.splitlines():
            stripped = ts_re.sub("", line).strip()
            if stripped:
                derived.append(stripped)
        if derived:
            plain = "\n".join(derived)
            print(f"[LYRICS] lrclib derived plain from synced "
                  f"({len(plain)} chars, {len(derived)} lines)")
    return {
        "plain": plain,
        "synced": synced,
        "duration": data.get("duration"),
    }


def _try_lrclib_search(artist: str, song: str) -> list:
    """GET /api/search?q=<artist> <song>. Endpoint fuzzy de lrclib.net
    que devuelve hasta N candidates (cada uno con el mismo shape que
    /api/get: id, trackName, artistName, plainLyrics, syncedLyrics,
    duration, instrumental).

    Best-effort: cualquier error (network, parsing, status != 200)
    devuelve [] sin raise. El caller decide qué hacer si no hay
    resultados (típicamente: caer al Gemini fallback original).
    """
    if not artist or not song:
        return []
    import requests as _req
    try:
        q = f"{artist} {song}".strip()
        r = _req.get(
            "https://lrclib.net/api/search",
            params={"q": q},
            timeout=8.0,
            headers={"User-Agent": "GenLyAI/1.0 (+https://app.genly.pro)"},
        )
        if r.status_code != 200:
            print(f"[LYRICS] lrclib /search {r.status_code} for q={q!r}")
            return []
        data = r.json()
        if not isinstance(data, list):
            return []
        return data
    except Exception as e:
        print(f"[LYRICS] lrclib /search failed: {e}")
        return []


def _pick_best_lrclib_candidate(candidates: list, artist: str,
                                 song: str) -> dict | None:
    """Scorea cada candidate de /api/search contra el (artist, song)
    pedido. Devuelve el de mayor score si supera el threshold 0.5,
    sino None.

    Scoring:
      - Artist match exacto: +0.5; substring: +0.3; else 0.
      - Song match exacto: +0.3; substring: +0.2; else 0.
      - Bonus +0.2 si el candidate tiene syncedLyrics (preferimos
        synced sobre plain para output con timestamps exactos).
      - Threshold 0.5: requiere mínimo artist+song match O synced+song
        match razonable. Evita aceptar matches débiles que generarían
        output peor que el Gemini fallback existente.
    """
    if not candidates:
        return None

    def _norm(s: str) -> str:
        return (s or "").lower().strip()

    artist_n = _norm(artist)
    song_n = _norm(song)
    if not artist_n or not song_n:
        return None

    best = None
    best_score = 0.0
    for c in candidates:
        if not isinstance(c, dict):
            continue
        c_artist = _norm(c.get("artistName"))
        c_song = _norm(c.get("trackName"))
        a_score = (
            0.5 if c_artist == artist_n
            else 0.3 if artist_n and (artist_n in c_artist or c_artist in artist_n)
            else 0.0
        )
        s_score = (
            0.3 if c_song == song_n
            else 0.2 if song_n and (song_n in c_song or c_song in song_n)
            else 0.0
        )
        sync_score = 0.2 if c.get("syncedLyrics") else 0.0
        score = a_score + s_score + sync_score
        if score > best_score:
            best_score = score
            best = c
    if best_score >= 0.5:
        return best
    return None


_LRC_LINE = None  # lazy-compiled regex


def _lrc_to_segments(lrc: str, audio_duration: float | None = None,
                     time_offset: float = 0.0) -> list[dict]:
    """Parse LRC-format synced lyrics into Whisper-shape segments.

    LRC line format: ``[mm:ss.xx] Text``. Empty-text lines (e.g. ``[00:06.00]``
    in the Karol G example) are gap markers that bound the previous segment
    but don't produce a segment of their own. Each emitted segment's `end`
    is set to the next line's `start` minus a tiny gap (50 ms), so subtitles
    leave the screen exactly when the next line should appear. Tail segment
    ends at audio_duration when known, otherwise +8 s after its start.

    `time_offset` shifts ALL timestamps by the given seconds — used when the
    user uploads a version of the song with extra audio at the start (e.g.
    "Official Video" with a dialogue intro that the studio LRC doesn't
    account for). The caller computes the offset by comparing user audio
    duration against lrclib's reported duration.
    """
    import re as _re
    global _LRC_LINE
    if _LRC_LINE is None:
        _LRC_LINE = _re.compile(r"^\s*\[(\d+):(\d+)(?:[.:](\d+))?\]\s*(.*)$")

    raw: list[dict] = []
    for line in (lrc or "").splitlines():
        m = _LRC_LINE.match(line)
        if not m:
            continue
        mm, ss, frac, text = m.group(1), m.group(2), m.group(3), m.group(4)
        try:
            start = int(mm) * 60 + int(ss)
            if frac:
                start += int(frac) / (10 ** len(frac))
        except ValueError:
            continue
        raw.append({"start": float(start), "text": (text or "").strip()})
    if not raw:
        return []
    raw.sort(key=lambda r: r["start"])

    segments: list[dict] = []
    n = len(raw)
    GAP = 0.05
    for i, item in enumerate(raw):
        if not item["text"]:
            continue  # gap marker — used only to bound the previous line
        # Find the next entry with a strictly greater start to set our end.
        j = i + 1
        while j < n and raw[j]["start"] <= item["start"]:
            j += 1
        if j < n:
            end = raw[j]["start"] - GAP
        elif audio_duration:
            end = min(float(audio_duration), item["start"] + 8.0)
        else:
            end = item["start"] + 5.0
        # Defensive: keep at least 0.5 s on screen
        if end < item["start"] + 0.5:
            end = item["start"] + 0.5
        segments.append({
            "start": item["start"] + time_offset,
            "end": end + time_offset,
            "text": item["text"],
        })
    return segments


def _detect_hallucination(segments: list[dict],
                           audio_duration: float | None) -> tuple[bool, str]:
    """Whisper hallucination smoke test. Returns (is_hallucinated, reason).

    Three independent signals trigger; ANY one is enough:
      - segment count is implausibly low for the audio duration,
      - any single segment is both very long (>15 s) and very wordy
        (>40 words) — the classic instrumental-passage trap,
      - any segment shows 3+ near-duplicate phrase windows by token-set
        Jaccard ≥ 0.75 — synonym loops ("reflexionar" ↔ "pensar") that
        the exact-match `_truncate_intra_loop` lets through.

    The detector is the GATE for the auto-recover branch: when this fires
    AND the caller has lrclib plain lyrics, we replace Whisper's output
    with synthesized segments (see `_synthesize_segments_from_plain`).
    """
    if not segments:
        return True, "empty segment list"

    # Signal 1 — segment count vs audio duration.
    if audio_duration and audio_duration > 30:
        minutes = audio_duration / 60.0
        floor = max(8, int(minutes * 4))
        if len(segments) < floor:
            return True, (f"low count: {len(segments)} segments for "
                          f"{audio_duration:.0f}s audio (floor={floor})")

    # Signal 2 — instrumental-passage mega-segment.
    for s in segments:
        dur = float(s.get("end", 0)) - float(s.get("start", 0))
        words = len((s.get("text") or "").split())
        if dur > 15.0 and words > 40:
            return True, (f"implausible segment: {dur:.1f}s × {words} "
                          f"words — text={(s.get('text') or '')[:60]!r}")

    # Signal 3 — fuzzy intra-loop (token-set Jaccard ≥ 0.75).
    for s in segments:
        if _has_fuzzy_intra_loop(s.get("text") or ""):
            return True, ("fuzzy intra-loop in segment "
                          f"{(s.get('text') or '')[:60]!r}")

    # Signal 4 — segment whose first and second halves carry the same
    # content. Catches the Whisper failure mode where the model emits
    # the SAME phrase exactly twice in one segment
    # ("¿Qué podía reflexionar sobre lo que estaba haciendo? ¿Qué
    # podía reflexionar sobre lo que estaba haciendo?") — only 2
    # repetitions so the fuzzy-loop check (3+) doesn't catch it.
    # Variety guards (each half needs >= 4 unique normalised tokens)
    # keep simple repetitive choruses like "la la la la" from being
    # false-flagged.
    for s in segments:
        text = s.get("text") or ""
        words = [n for n in (_normalize_token(w) for w in text.split()) if n]
        n = len(words)
        if n < 12:
            continue
        half = n // 2
        first_half = set(words[:half])
        second_half = set(words[half:half * 2])
        if len(first_half) < 4 or len(second_half) < 4:
            continue
        inter = len(first_half & second_half)
        union = len(first_half | second_half)
        if union > 0 and (inter / union) >= 0.85:
            dur = float(s.get("end", 0)) - float(s.get("start", 0))
            return True, (f"duplicate halves in {dur:.1f}s segment "
                          f"({n} words): {text[:60]!r}")

    return False, ""


def _normalize_token(s: str) -> str:
    """Lowercase + strip combining diacritics + drop non-alphanumeric.
    Without this, "haciendo," and "haciendo" or "podía" and "podia"
    register as distinct tokens, breaking the Jaccard fuzzy-loop check
    on real Whisper output (which carries Spanish accents and clause
    punctuation). The normalisation matches the behaviour Whisper users
    intuitively expect when reasoning about repetition."""
    import unicodedata as _u
    s = (s or "").lower()
    s = _u.normalize("NFD", s)
    return "".join(c for c in s if c.isalnum() and not _u.combining(c))


def _has_fuzzy_intra_loop(text: str) -> bool:
    """Detect 3+ near-duplicate consecutive word-windows in a segment.
    Two windows count as the same loop when their token-set Jaccard is
    ≥ 0.75 — catches synonym swaps ("reflexionar" ↔ "pensar") that the
    exact-equality intra-loop truncator misses.

    Window sizes 4..14 (longer first), same shape as the existing
    `_truncate_intra_loop`, but only used as a SIGNAL here, not a fix.

    Tokens are normalised (lowercase + accent fold + punctuation strip)
    before comparison. Earlier versions used `.lower()` only and missed
    real-world Whisper hallucinations like "que podía reflexionar sobre
    lo que estaba haciendo, que podía pensar sobre lo que estaba
    haciendo …" because "haciendo," and "haciendo" tokenised
    differently.
    """
    raw = text.split()
    words = [n for n in (_normalize_token(w) for w in raw) if n]
    total = len(words)
    if total < 12:
        return False
    for window in range(14, 3, -1):
        if total < window * 3:
            continue
        for start in range(total - window * 3 + 1):
            phrase_set = set(words[start:start + window])
            if not phrase_set:
                continue
            count = 1
            pos = start + window
            while pos + window <= total:
                next_set = set(words[pos:pos + window])
                if not next_set:
                    break
                inter = len(phrase_set & next_set)
                union = len(phrase_set | next_set)
                if union == 0 or (inter / union) < 0.75:
                    break
                count += 1
                pos += window
            if count >= 3:
                return True
    return False


# Section markers we strip when distributing lrclib plain lyrics — they're
# scaffolding metadata, not lines a singer actually performs.
_PLAIN_SECTION_MARKER = re.compile(
    r"^\s*[\[(](?:verso|verse|coro|chorus|estribillo|puente|bridge|"
    r"intro|outro|pre[- ]?coro|pre[- ]?chorus|interlude|instrumental|"
    r"refr[áa]n|solo)[^\]\)]*[\])]\s*$",
    re.IGNORECASE,
)


def _split_plain_lines(plain: str) -> list[str]:
    """Split lrclib plain text into singable lines.
    Drops empties + section markers ([Verso], [Chorus], etc.) so the
    output is exactly the lines a vocalist actually performs.
    """
    if not plain:
        return []
    out: list[str] = []
    for raw in plain.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        if _PLAIN_SECTION_MARKER.match(stripped):
            continue
        out.append(stripped)
    return out


def _align_whisper_to_plain(segments: list[dict],
                             plain: str) -> list[tuple[int, float]]:
    """Find time anchors by fuzzy-matching Whisper's surviving segments
    against lrclib plain lyric lines.

    Even when Whisper hallucinates the back half of a song, the FIRST
    segments are usually correct — they anchor onto real audio cues.
    We can use those segments as time landmarks: "Whisper heard this
    text at 0.2s; that text matches plain line 0; therefore line 0
    starts at 0.2s." With multiple anchors, the synthesizer interpolates
    the rest of the lyric lines piecewise instead of distributing them
    uniformly across the full duration. Result: timestamps land much
    closer to the actual singing without any operator effort.

    Returns sorted list of (line_index, time_seconds) tuples. Each
    anchor satisfies:
      - the segment passes the per-segment hallucination signals
        (no mega-segment, no fuzzy intra-loop),
      - it fuzzy-matches a plain line with token-set Jaccard ≥ 0.3,
      - and its line index is strictly greater than every prior anchor's
        (a later-in-time anchor that matches an EARLIER lyric line is
        almost certainly a wrong match — we drop it rather than confuse
        the interpolation).

    Empty list when no segment qualifies — caller falls back to uniform
    distribution from 0.
    """
    plain_lines = _split_plain_lines(plain)
    if not segments or not plain_lines:
        return []

    # Use the same normalisation as _has_fuzzy_intra_loop: lowercase +
    # accent fold + punctuation strip. Otherwise "podía" / "podia" or
    # "haciendo," / "haciendo" register as distinct tokens and the
    # Jaccard match score collapses below the 0.3 threshold.
    plain_token_sets = [
        {n for n in (_normalize_token(w) for w in line.split()) if n}
        for line in plain_lines
    ]

    raw: list[tuple[int, float]] = []
    for s in segments:
        text = (s.get("text") or "").strip()
        if not text:
            continue
        # Per-segment plausibility — same signals the global detector
        # uses. With no audio_duration only the mega-segment + fuzzy-loop
        # checks fire.
        per_seg_bad, _ = _detect_hallucination([s], audio_duration=None)
        if per_seg_bad:
            continue
        seg_set = {n for n in (_normalize_token(w) for w in text.split()) if n}
        if not seg_set:
            continue
        best_idx = -1
        best_score = 0.0
        for i, p_set in enumerate(plain_token_sets):
            if not p_set:
                continue
            inter = len(seg_set & p_set)
            union = len(seg_set | p_set)
            if union == 0:
                continue
            score = inter / union
            if score > best_score:
                best_score = score
                best_idx = i
        if best_idx >= 0 and best_score >= 0.3:
            raw.append((best_idx, float(s.get("start", 0.0))))

    raw.sort(key=lambda a: a[1])
    # Monotonic filter: drop later-in-time anchors that point earlier in
    # the lyrics (almost always a bad match).
    filtered: list[tuple[int, float]] = []
    last_idx = -1
    for idx, t in raw:
        if idx > last_idx:
            filtered.append((idx, t))
            last_idx = idx
    return filtered


def _synthesize_segments_from_plain(plain: str,
                                     audio_duration: float,
                                     anchors: list[tuple[int, float]] | None = None,
                                     start_time: float = 0.0,
                                     ) -> list[dict]:
    """Distribute lrclib plain lyrics across the audio duration.

    Used when Whisper has hallucinated and we need to ship the operator
    a complete transcription instead of 3 broken rows. With no anchors
    we distribute lines uniformly; with anchors we interpolate piecewise
    between (line_index, time) points so each lyric line lands near the
    moment Whisper actually heard it.

    Args:
        plain: lrclib plain text, one line per lyric line. Section
            markers like "[Verso]" / "[Chorus]" are filtered out.
        audio_duration: total audio length in seconds.
        anchors: optional list of (line_index, time_seconds) pairs from
            `_align_whisper_to_plain`. Empty/None falls back to even
            distribution from `start_time`.
        start_time: where the song body actually starts in the user's
            audio. Default 0 (whole audio is song). When the audio has
            a spoken-intro / dialogue prefix that the lrclib studio
            version doesn't have (e.g. the YouTube "Video Oficial" cut
            of "El Plan de la Mariposa - El Riesgo" has 73 s of
            dialogue before the song proper begins), the caller passes
            `start_time=intro_offset` so the synthesized song lyrics
            distribute over [intro_offset, audio_duration] instead of
            getting compressed by the spoken intro region.

    Returns segments in the same shape as `transcribe()` — list of
    {start, end, text} dicts, monotonically increasing, last `end`
    capped at `audio_duration`.
    """
    if not plain or not plain.strip() or not audio_duration:
        return []

    lines = _split_plain_lines(plain)
    if not lines:
        return []
    n = len(lines)

    # Filter / dedupe anchors to keep them strictly inside the line+time
    # window and strictly monotonic. The aligner already does this, but
    # we re-check defensively in case the caller hand-built anchors.
    monotonic: list[tuple[float, float]] = []
    last_idx_f, last_t_f = -1.0, -1.0
    for raw_anchor in (anchors or []):
        try:
            idx, t = float(raw_anchor[0]), float(raw_anchor[1])
        except (TypeError, ValueError, IndexError):
            continue
        if not (0 <= idx < n):
            continue
        if not (0 <= t < audio_duration):
            continue
        if idx > last_idx_f and t > last_t_f:
            monotonic.append((idx, t))
            last_idx_f, last_t_f = idx, t

    # Build the piecewise interpolation table. Always end at
    # (n, audio_duration); start at (0, start_time) unless an anchor
    # lives at line 0. start_time defaults to 0 (whole audio is song);
    # for tracks with a non-song prefix (spoken intro, dialogue) the
    # caller passes intro_offset so the song lyrics distribute over
    # the song region only.
    safe_start = max(0.0, min(float(start_time), float(audio_duration) - 0.5))
    points: list[tuple[float, float]] = list(monotonic)
    if not points or points[0][0] > 0:
        points.insert(0, (0.0, safe_start))
    if points[-1][0] < float(n):
        points.append((float(n), float(audio_duration)))

    def _time_at(line_index: float) -> float:
        for (l1, t1), (l2, t2) in zip(points, points[1:]):
            if line_index <= l2:
                if l2 == l1:
                    return t1
                return t1 + (line_index - l1) / (l2 - l1) * (t2 - t1)
        return points[-1][1]

    GAP = 0.05
    segments: list[dict] = []
    for i, line in enumerate(lines):
        start = _time_at(float(i))
        end = _time_at(float(i + 1)) - GAP
        if end <= start:
            end = start + 0.5
        segments.append({"start": start, "end": end, "text": line})
    if segments and segments[-1]["end"] > audio_duration:
        segments[-1]["end"] = audio_duration
    return segments


def _detect_speech_regions(audio_path: str,
                            top_db: float = 30.0,
                            min_region_s: float = 0.4,
                            merge_gap_s: float = 0.5,
                            ) -> list[tuple[float, float]]:
    """Return non-silent intervals from the audio, in seconds.

    Uses librosa's energy-based VAD (`effects.split` on the loaded
    waveform with a `top_db` threshold below the peak). Generic — works
    for any song. Output is what we use to decide WHERE in the audio
    a vocal subtitle could legitimately be placed; gap-fill avoids
    placing reference lines inside long instrumental silences.

    Defaults tuned for music (top_db=30 keeps quiet vocals in but drops
    drum-kit-only regions). Returns [] on any failure so the caller
    can fall back to time-uniform distribution.
    """
    try:
        import librosa
        import numpy as np
        # Mono, native rate sufficient for VAD; 22 kHz is librosa default.
        y, sr = librosa.load(audio_path, sr=22050, mono=True)
        intervals = librosa.effects.split(y, top_db=top_db)
        regions: list[tuple[float, float]] = []
        for start_sample, end_sample in intervals:
            start = float(start_sample) / sr
            end = float(end_sample) / sr
            if end - start < min_region_s:
                continue  # too short — likely click/spike, not speech
            regions.append((start, end))
        # Merge regions separated by very small gaps (single breath
        # between two phrases) so consecutive vocal phrases don't get
        # split into N micro-regions.
        if not regions:
            return []
        merged: list[tuple[float, float]] = [regions[0]]
        for start, end in regions[1:]:
            prev_start, prev_end = merged[-1]
            if start - prev_end <= merge_gap_s:
                merged[-1] = (prev_start, end)
            else:
                merged.append((start, end))
        return merged
    except Exception as e:
        print(f"[VAD] _detect_speech_regions failed ({e}); skipping VAD")
        return []


def _fill_gaps_with_reference(whisper_segments: list[dict],
                               reference: str,
                               audio_duration: float,
                               coverage_threshold: float = 0.7,
                               audio_path: str | None = None,
                               ) -> list[dict] | None:
    """Generic recovery for outlier songs: keep Whisper's plausible
    segments, then fill the uncovered time intervals with lines from
    the reference text distributed proportionally.

    This is the right model whenever Whisper returns SOME real
    transcription (e.g. a spoken-dialogue intro that captures real
    words at real timestamps) interleaved with hallucinated segments
    (instrumental-passage mega-segments, synonym intra-loops). We
    must not throw the real segments away.

    Returns the merged segment list, or None when there's nothing
    sensible to return (no plausible Whisper AND no reference). The
    caller can decide whether to surface a coverage_warning.

    `coverage_threshold`: when the kept Whisper segments cover more
    than this fraction of the audio, return them as-is (no synthesis
    needed — Whisper worked). Default 0.7.

    Used by the Gemini-fallback path in /transcribe where lrclib was
    unavailable and we therefore don't know intro_offset. The lrclib-
    plain branch uses a different (more accurate) flow because it
    knows the song-body offset.
    """
    if not audio_duration or audio_duration <= 0:
        return whisper_segments or None

    # 1. Keep only segments that pass per-segment plausibility.
    kept: list[dict] = []
    dropped = 0
    for s in (whisper_segments or []):
        bad, _ = _detect_hallucination([s], audio_duration=None)
        if bad:
            dropped += 1
            continue
        kept.append(s)
    kept.sort(key=lambda x: float(x.get("start", 0)))

    coverage = sum(
        float(s.get("end", 0)) - float(s.get("start", 0)) for s in kept
    ) / float(audio_duration)

    # 2. Whisper covers most of the audio → no synthesis needed.
    if coverage >= coverage_threshold:
        return kept

    # 3. Sparse coverage: distribute reference lines into the gaps.
    ref_lines = _split_plain_lines(reference) if reference else []
    if not ref_lines:
        # Nothing to synthesize from. Return the (possibly empty) kept
        # set; caller falls back to whatever default it had.
        return kept or None

    # Build the gap list. We start from one of two sources:
    #   - VAD-detected SPEECH regions (preferred when audio_path is
    #     supplied) — distributing reference lines only where someone
    #     is actually singing/speaking. This is the right model for
    #     songs with long instrumental sections where uniform fill
    #     would land subtitles in silence (verified failure mode for
    #     "El Plan de la Mariposa - El Riesgo": 73 s spoken intro,
    #     instrumental gaps, then sung body).
    #   - Whole-audio gaps (legacy path) when no audio_path is given.
    # Each "gap" then has the time spans of any kept Whisper segments
    # subtracted from it so we don't double up subtitles in the same
    # window.
    speech_regions: list[tuple[float, float]] = []
    if audio_path:
        speech_regions = _detect_speech_regions(audio_path)
        if speech_regions:
            print(f"[VAD] {len(speech_regions)} speech regions detected; "
                  f"reference will be distributed inside them")
    if not speech_regions:
        speech_regions = [(0.0, float(audio_duration))]

    # Subtract kept Whisper time-windows from each speech region so
    # we don't synthesize over a real Whisper segment.
    kept_intervals = sorted(
        (float(s["start"]), float(s["end"])) for s in kept
    )

    def _subtract_kept(start: float, end: float) -> list[tuple[float, float]]:
        out: list[tuple[float, float]] = []
        cur = start
        for ks, ke in kept_intervals:
            if ke <= cur or ks >= end:
                continue
            if ks > cur:
                out.append((cur, min(ks, end)))
            cur = max(cur, ke)
            if cur >= end:
                break
        if cur < end:
            out.append((cur, end))
        return out

    gaps: list[tuple[float, float]] = []
    for region_start, region_end in speech_regions:
        for sub_start, sub_end in _subtract_kept(region_start, region_end):
            if sub_end - sub_start >= 1.0:
                gaps.append((sub_start, sub_end))

    if not gaps:
        return kept

    total_gap = sum(end - start for start, end in gaps)
    if total_gap <= 0:
        return kept

    # 4. Allocate reference lines per gap, proportional to gap duration.
    #
    # We use the largest-remainder method (Hamilton method) to guarantee
    # `sum(allocations) == n_lines` exactly, regardless of how the floats
    # round. Old `round()`-per-gap allocation could:
    #   - sum to > n_lines and starve the final gap into negative remainder,
    #   - sum to << n_lines for songs with one big gap and many tiny ones,
    #     piling lines into the trailing gap.
    n_lines = len(ref_lines)
    GAP_BETWEEN = 0.05
    audio_dur_f = float(audio_duration)
    output = list(kept)

    floor_alloc = [int((end - start) / total_gap * n_lines) for (start, end) in gaps]
    fracs = [
        ((end - start) / total_gap * n_lines) - floor_alloc[i]
        for i, (start, end) in enumerate(gaps)
    ]
    leftover = n_lines - sum(floor_alloc)
    # Distribute one extra line at a time to the gap with the largest
    # fractional part. Tiebreak by gap index for determinism.
    if leftover > 0:
        ranked = sorted(range(len(gaps)), key=lambda i: (-fracs[i], i))
        for idx in ranked[:leftover]:
            floor_alloc[idx] += 1
    # `leftover` cannot be negative under largest-remainder, but guard
    # defensively against fp drift on degenerate inputs.
    elif leftover < 0:
        ranked = sorted(range(len(gaps)), key=lambda i: (fracs[i], i))
        for idx in ranked[:(-leftover)]:
            if floor_alloc[idx] > 0:
                floor_alloc[idx] -= 1

    line_cursor = 0
    for i, (start, end) in enumerate(gaps):
        line_count = floor_alloc[i]
        if line_count <= 0:
            continue
        gap_dur = end - start
        per_line = gap_dur / line_count
        for j in range(line_count):
            line_idx = line_cursor + j
            if line_idx >= n_lines:
                break
            # Clamp BOTH start and end to [0, audio_duration]. Old code
            # only clamped end, which let `start > audio_duration` slip
            # through and produce inverted segments.
            line_start = max(0.0, min(start + j * per_line, audio_dur_f))
            line_end = min(start + (j + 1) * per_line - GAP_BETWEEN, audio_dur_f)
            if line_end <= line_start:
                # Drop degenerate (zero-or-negative duration) segments
                # outright. Pre-fix code synthesized a 0.5 s pad here,
                # which the renderer then drew on top of the next segment.
                continue
            output.append({
                "start": line_start,
                "end": line_end,
                "text": ref_lines[line_idx],
            })
        line_cursor += line_count

    output.sort(key=lambda s: float(s["start"]))
    return output


def _audio_duration(audio_path: str) -> float | None:
    """Best-effort audio duration in seconds. Handles both MP3 and WAV.
    For MP3 we use mutagen.mp3 (header-only, ~1 ms). For WAV we use the
    stdlib `wave` module (also header-only). Falls back to moviepy
    (slower, opens the full file) on any failure. Returns None if
    everything fails."""
    name_lower = audio_path.lower()
    if name_lower.endswith(".mp3"):
        try:
            from mutagen.mp3 import MP3
            return float(MP3(audio_path).info.length)
        except Exception:
            pass
    elif name_lower.endswith(".wav"):
        try:
            import wave
            with wave.open(audio_path, "rb") as wf:
                frames = wf.getnframes()
                rate = wf.getframerate() or 0
                if rate > 0:
                    return float(frames) / rate
        except Exception:
            pass
    try:
        from moviepy.editor import AudioFileClip
        with AudioFileClip(audio_path) as a:
            return float(a.duration)
    except Exception:
        return None


def _slice_audio_window(input_path: str, output_path: str,
                         start_seconds: float, duration_seconds: float) -> bool:
    """Slice an arbitrary [start, start+duration] window from an MP3.

    Uses ``-ss`` AFTER ``-i`` for sample-accurate seek (slow seek), and
    re-encodes via libmp3lame so we don't depend on keyframe alignment.
    Slower than ``_slice_audio_prefix`` (re-encode vs stream copy) but
    more reliable for arbitrary offsets where MP3 frame boundaries may
    not line up with the requested cut.

    Returns True on success, False on any failure. Best-effort.
    """
    if start_seconds < 0 or duration_seconds <= 0:
        return False
    import subprocess as _sp
    try:
        _sp.run(
            ["ffmpeg", "-y", "-i", input_path,
             "-ss", str(start_seconds), "-t", str(duration_seconds),
             "-acodec", "libmp3lame", "-q:a", "5",
             "-loglevel", "error", output_path],
            check=True, timeout=30,
        )
        return os.path.exists(output_path) and os.path.getsize(output_path) > 0
    except (_sp.CalledProcessError, _sp.TimeoutExpired, FileNotFoundError, OSError) as e:
        print(f"[LYRICS] _slice_audio_window failed: {e}")
        return False


def _whisper_quick_text(mp3_path: str, job_id: str | None = None) -> str:
    """Minimal whisper-1 transcription of a short clip — used by alignment
    verification. Returns plain text with no post-processing (no spam
    filter, no dedup). Best-effort: returns "" on any failure.

    `job_id` is optional; when provided, the call gets recorded in
    ai_provenance so the cost dashboard counts it. These clips are
    short (~5 s) so the cost is ~$0.0005 each, but at scale across
    every job the cents add up.
    """
    if not os.path.exists(mp3_path):
        return ""
    recorder = None
    if job_id:
        from provenance import record_ai_call
        recorder = record_ai_call(
            job_id=job_id,
            step="whisper_quick_align",
            tool_name="whisper-1",
            tool_provider="openai",
            prompt="(audio alignment short clip)",
            input_data_types=["audio_file_short"],
        )
    try:
        from openai import OpenAI
        with open(mp3_path, "rb") as f:
            r = OpenAI().audio.transcriptions.create(
                model="whisper-1", file=f, response_format="text",
            )
        if recorder is not None:
            try:
                recorder.finish(response_summary="whisper_quick_ok")
            except Exception:
                pass
        return (r or "").strip()
    except Exception as e:
        print(f"[LYRICS] _whisper_quick_text failed: {e}")
        return ""


def _verify_lrclib_alignment(audio_path: str, expected_text: str,
                              claimed_start: float, window: float = 5.5) -> float | None:
    """Slice a ~window-second clip of audio starting just before
    `claimed_start`, run Whisper on it, fuzzy-match against `expected_text`.

    Returns a similarity ratio in [0, 1] (1.0 = identical, 0 = nothing in
    common), or None if slicing or Whisper failed and we cannot verify.

    Used to confirm lrclib's offset-shifted timestamps actually line up
    with what's being sung in the user's audio. Cheap (~3 s, ~$0.0005).
    Conservative threshold for "trust": ~0.4. For UMG-style operator
    review, this is the difference between "subtitles look right" and
    "subtitles are 30 s off and we shipped it."
    """
    if claimed_start < 0 or not expected_text:
        return None
    import tempfile
    from difflib import SequenceMatcher
    import re as _re

    fd, clip_path = tempfile.mkstemp(suffix=".mp3")
    try:
        os.close(fd)
        slice_start = max(0.0, claimed_start - 0.3)
        if not _slice_audio_window(audio_path, clip_path, slice_start, window):
            return None
        actual = _whisper_quick_text(clip_path)
        if not actual:
            return None
        def _norm(s: str) -> str:
            return _re.sub(r"[^\w\s]", "", s.lower()).strip()
        return SequenceMatcher(None, _norm(actual), _norm(expected_text)).ratio()
    finally:
        try:
            os.unlink(clip_path)
        except OSError:
            pass


def _slice_audio_prefix(input_path: str, output_path: str, seconds: float) -> bool:
    """Slice the first ``seconds`` of an MP3 into ``output_path`` using ffmpeg.

    Used when the user uploads a song version with extra audio at the start
    (a dialogue intro on an "Official Video" cut, e.g.) — we slice that
    intro chunk and feed it to Whisper separately so the operator gets a
    transcription of the dialogue too. The song proper is timestamped from
    lrclib's synced lyrics with an offset.

    Uses ``-acodec copy`` so there is no re-encode — just a stream copy of
    the audio bytes through the cut point. Fast (< 1 s for typical sizes).

    Returns True on success, False on any failure. Best-effort: caller
    treats False as "no intro transcription available" and continues.
    """
    if seconds <= 0:
        return False
    import subprocess as _sp
    try:
        _sp.run(
            ["ffmpeg", "-y", "-i", input_path,
             "-t", str(seconds), "-acodec", "copy",
             "-loglevel", "error", output_path],
            check=True, timeout=30,
        )
        return os.path.exists(output_path) and os.path.getsize(output_path) > 0
    except (_sp.CalledProcessError, _sp.TimeoutExpired, FileNotFoundError, OSError) as e:
        print(f"[LYRICS] _slice_audio_prefix failed: {e}")
        return False


def _sanitize_gemini_lyrics(text):
    """Strip section/pilcrow markers that some Spanish lyrics sites use
    as estrofa separators (Letras.com, AZLyrics, etc.). These are HTML
    structure artifacts from scraping — they never appear in the actual
    sung lyrics.

    Why this matters: the cleaned text is used in two downstream paths
    that both fail when these chars leak through:
      1. Cached into lyrics_cache.lyrics — the row gets returned to all
         future callers including the lyrics_hint primer for Whisper.
      2. Passed as Whisper's `prompt` parameter — when the prompt
         contains `§`, Whisper biases toward emitting `§` in its own
         transcription output, which then lands in jobs.segments_json
         and renders as visible text in the lyric video (root cause
         of the Mujer Amante / Rata Blanca incident, 2026-05-12).

    Strictly conservative: removes only U+00A7 SECTION SIGN and U+00B6
    PILCROW. Diacritics, em-dashes, Spanish quotes, and every other
    char that legitimately appears in lyrics are preserved.
    """
    if not text:
        return text
    cleaned = text.replace("§", "").replace("¶", "")
    if cleaned != text:
        stripped = len(text) - len(cleaned)
        # Logged at WARNING so the operator can see which Gemini-grounded
        # sources keep returning these chars — over time, this surfaces
        # which lyric sites are dirty and whether the sanitizer needs
        # to grow (e.g. another scraping artifact appears).
        print(f"[lyrics_sanitize] stripped {stripped} char(s) (§/¶) "
              f"from Gemini response")
    return cleaned


def _fetch_lyrics_via_gemini_search(
    artist: str, song: str,
    job_id: str | None = None,
    db=None,
) -> str | None:
    """Fetch reference lyrics for (artist, song) via Gemini 2.5 Flash with
    the google_search grounding tool. Returns plain-text lyrics on success,
    None on cache miss + fetch failure + validation reject.

    Best-effort — never raises. The /transcribe endpoint falls through to
    lyrics.ovh when this returns None.

    Provenance: caller passes `job_id` to record an AIProvenance row keyed
    to that job (UMG audit trail). When called from /transcribe (pre-job),
    job_id=None and the LyricsCache row itself serves as the audit record
    (timestamp + source URLs + model name).
    """
    if not artist or not song:
        return None

    # Kill switch — flip to false in Railway if Gemini path misbehaves in prod.
    if not _truthy_env(os.environ.get("LYRICS_GEMINI_SEARCH_ENABLED", "true")):
        return None

    cache_key = _lyrics_cache_key(artist, song)

    # Cache lookup (Postgres — shared across the worker fleet).
    if db is not None:
        try:
            from database import LyricsCache
            row = db.query(LyricsCache).filter(
                LyricsCache.cache_key == cache_key
            ).first()
            if row and row.lyrics:
                print(f"[LYRICS] cache hit {cache_key} ({len(row.lyrics)} chars)")
                # Sanitize on read so existing poisoned rows (cached
                # before this fix shipped) still return clean text to
                # downstream callers without requiring a DB cleanup.
                return _sanitize_gemini_lyrics(row.lyrics)
        except Exception as e:
            print(f"[LYRICS] cache read failed: {e}")

    # Build Gemini call.
    from google import genai
    from google.genai import types
    from provenance import record_ai_call

    system_prompt = (
        "You are a lyrics retrieval assistant. Use the google_search tool to "
        "find the official lyrics of a song from public lyrics websites "
        "(genius.com, letras.com, azlyrics.com, lyrics.com, musixmatch.com, "
        "songmeanings.com). Return ONLY the lyrics as plain text, one line "
        "per song line. No commentary, no bracketed section headers like "
        "[Chorus] or [Verse], no translation, no annotations. "
        "If you cannot verify the lyrics from a lyrics website, respond "
        "exactly with: LYRICS_NOT_FOUND"
    )
    user_content = f'Find the lyrics for the song "{song}" by {artist}.'
    full_prompt = f"system:{system_prompt}\nuser:{user_content}"

    recorder = record_ai_call(
        job_id=job_id,
        step="lyrics_reference_fetch",
        tool_name="gemini-2.5-flash",
        tool_provider="google_vertex",
        tool_version=getattr(genai, "__version__", None),
        prompt=full_prompt,
        input_data_types=["artist_name", "song_title"],
    ) if job_id else None

    try:
        client = _get_genai_client()
        search_tool = types.Tool(google_search=types.GoogleSearch())
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=user_content,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                tools=[search_tool],
                temperature=0.1,
                max_output_tokens=2000,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )

        text = ""
        try:
            text = (response.text or "").strip()
        except Exception:
            text = ""

        # Sanitize ONCE here, before everything downstream sees the text.
        # Some Spanish lyrics sites (Letras.com, AZLyrics) use § as estrofa
        # separators; the Gemini scrape leaks them into our string. Without
        # this strip the text would land in lyrics_cache.lyrics AND be
        # passed as Whisper's prompt parameter, biasing transcription to
        # emit § in segments_json. See _sanitize_gemini_lyrics for context.
        text = _sanitize_gemini_lyrics(text)

        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            if recorder:
                recorder.finish(response_summary="no_candidates")
            return None
        cand = candidates[0]
        finish_reason = getattr(cand, "finish_reason", None)
        finish_str = str(finish_reason) if finish_reason is not None else ""

        # Gemini blocks copyrighted recitation aggressively. Degrade silently.
        if "RECITATION" in finish_str or "SAFETY" in finish_str:
            print(f"[LYRICS] gemini blocked: finish_reason={finish_str}")
            if recorder:
                recorder.finish(response_summary=f"blocked={finish_str}")
            return None
        if not text or text.strip() == "LYRICS_NOT_FOUND":
            if recorder:
                recorder.finish(response_summary=f"empty_or_sentinel; finish={finish_str}")
            return None

        # Extract grounding sources (proves the answer was grounded, not
        # purely hallucinated from training data).
        gm = getattr(cand, "grounding_metadata", None)
        chunks = getattr(gm, "grounding_chunks", None) or []
        source_urls: list[str] = []
        source_titles: list[str] = []
        for c in chunks:
            web = getattr(c, "web", None)
            if not web:
                continue
            uri = getattr(web, "uri", None)
            title = getattr(web, "title", None)
            if uri:
                source_urls.append(uri)
            if title:
                source_titles.append(title)

        if not source_urls:
            print("[LYRICS] no grounding sources — refusing to trust ungrounded text")
            if recorder:
                recorder.finish(response_summary="no_grounding_sources")
            return None

        # Soft signal: did any grounding chunk hit a known lyric site?
        on_lyric_site = False
        haystack = " ".join(source_urls + source_titles).lower()
        for d in _LYRIC_DOMAINS:
            if d in haystack:
                on_lyric_site = True
                break
        if not on_lyric_site:
            print(f"[LYRICS] grounding off lyric-domain allow-list (soft warn): "
                  f"{source_urls[:2]}")

        # Lyrics-shape validation.
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if len(lines) < 8:
            if recorder:
                recorder.finish(response_summary=f"too_few_lines={len(lines)}")
            return None
        if len(text) < 80:
            if recorder:
                recorder.finish(response_summary=f"too_short_chars={len(text)}")
            return None

        # Merged-line quality guard. Algunos sitios de letras devuelven
        # las estrofas como párrafos largos en vez de líneas separadas
        # (ej. Letras.com en flow mode). Si Gemini scrapea ese formato,
        # avg chars/line se dispara (paragraph-style ~80+ chars vs
        # lyric-style ~20-40 chars). Cuando este texto se usa como
        # `lyrics_hint` de Whisper o como reference para gap-fill, el
        # output queda mergeado de a 2-3 líneas con timestamps mal
        # distribuidos (caso real: Noches Sin Sueño Rata Blanca en
        # staging, Gemini devolvió 439 chars / 12 lines = 36.6 chars/line
        # cuando lrclib synced tenía ~30 líneas para esa canción).
        #
        # Threshold 50: lyric lines típicas de pop/rock/balada son 20-40
        # chars. 50+ es signature de merged-stanza scraping. Rechazar
        # es preferible a contaminar — el caller cae al path de Whisper
        # sin hint en vez de Whisper sesgado con merged-text.
        avg_chars_per_line = sum(len(l) for l in lines) / len(lines)
        if avg_chars_per_line > 50.0:
            print(f"[LYRICS] gemini output looks merged "
                  f"(avg {avg_chars_per_line:.1f} chars/line over "
                  f"{len(lines)} lines) — rejecting")
            if recorder:
                recorder.finish(
                    response_summary=f"rejected_merged_lines="
                                     f"{avg_chars_per_line:.1f}cpl/{len(lines)}",
                )
            return None

        # Repetition guard — Gemini hallucination loops on a single line.
        from collections import Counter
        most_common, mc_count = Counter(lines).most_common(1)[0]
        if mc_count / len(lines) > 0.4:
            if recorder:
                recorder.finish(response_summary=f"repetition={mc_count}/{len(lines)}")
            return None

        # Persist to cache.
        if db is not None:
            try:
                from database import LyricsCache
                row = db.query(LyricsCache).filter(
                    LyricsCache.cache_key == cache_key
                ).first()
                if row is None:
                    row = LyricsCache(
                        cache_key=cache_key,
                        artist=artist[:255],
                        title=song[:255],
                        lyrics=text,
                        source_urls=source_urls[:20],
                        fetched_by_model="gemini-2.5-flash",
                    )
                    db.add(row)
                    db.commit()
                # If row already exists (race), keep existing — first writer wins.
            except Exception as e:
                print(f"[LYRICS] cache write failed: {e}")
                try:
                    db.rollback()
                except Exception:
                    pass

        if recorder:
            try:
                summary = json.dumps({
                    "lyrics_chars": len(text),
                    "lyrics_lines": len(lines),
                    "distinct_lines": len(set(lines)),
                    "grounding_sources": source_urls[:10],
                    "grounding_titles": source_titles[:10],
                    "on_lyric_site_allowlist": on_lyric_site,
                    "finish_reason": finish_str,
                    "validation_passed": True,
                })[:2000]
            except Exception:
                summary = (f"chars={len(text)} lines={len(lines)} "
                           f"grounding={len(source_urls)}")
            recorder.finish(
                response_summary=summary,
                output_artifact=f"lyrics_cache:{cache_key}",
            )

        print(f"[LYRICS] gemini fetched {len(text)} chars / {len(lines)} lines / "
              f"{len(source_urls)} sources for {artist!r} - {song!r}")
        return text

    except Exception as e:
        print(f"[LYRICS] gemini search failed: {e}")
        if recorder:
            try:
                recorder.finish(response_summary=f"error: {str(e)[:200]}")
            except Exception:
                pass
        return None


def _fetch_lyrics_from_sources(
    artist: str, song: str,
    job_id: str | None = None,
    db=None,
) -> list[str]:
    """Backward-compat wrapper used by callers that still expect list[str].

    /transcribe in main.py now calls _fetch_lyrics_via_gemini_search directly
    (it needs the parallel-with-Whisper kickoff), but this wrapper stays so
    any future caller that wants a single function call still works.
    """
    text = _fetch_lyrics_via_gemini_search(artist, song, job_id=job_id, db=db)
    return [text] if text else []


# ---------------------------------------------------------------------------
# Step 1.5 — AI Background Generation (Google Veo 3 → SD fallback)
# ---------------------------------------------------------------------------

_VERTEX_CREDENTIALS = os.environ.get(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(__file__), "vertex_credentials.json"),
)
_VERTEX_PROJECT = os.environ.get("VERTEX_PROJECT", "gen-lang-client-0900526123")
_VERTEX_LOCATION = os.environ.get("VERTEX_LOCATION", "us-central1")

# Set credentials env var for Google SDK
os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", _VERTEX_CREDENTIALS)

_genai_client = None


def _get_genai_client():
    """Get a cached Vertex AI GenAI client.

    We pass credentials EXPLICITLY (not relying on the SDK's default
    application-default-credentials discovery) because Railway's container
    environment has been triggering "invalid_scope: Invalid OAuth scope or
    ID token audience provided" with default discovery — the SDK's auth
    chain ends up requesting an ID token instead of an OAuth2 access token,
    or hits a regional endpoint that rejects the default scope.

    Building Credentials.from_service_account_file with explicit
    cloud-platform scope gives us a normal OAuth2 access token that all
    Vertex endpoints accept. Same credentials work locally — the explicit
    binding just removes the SDK's environment guesswork.
    """
    global _genai_client
    if _genai_client is None:
        from google import genai
        from google.oauth2 import service_account

        creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
        print(f"[VERTEX] google-genai version: {genai.__version__}")
        print(f"[VERTEX] project={_VERTEX_PROJECT} location={_VERTEX_LOCATION}")
        print(f"[VERTEX] credentials path: {creds_path}")
        print(f"[VERTEX] credentials exists: {os.path.exists(creds_path)}")

        client_kwargs = dict(
            vertexai=True,
            project=_VERTEX_PROJECT,
            location=_VERTEX_LOCATION,
        )
        if creds_path and os.path.exists(creds_path):
            try:
                credentials = service_account.Credentials.from_service_account_file(
                    creds_path,
                    scopes=["https://www.googleapis.com/auth/cloud-platform"],
                )
                # Bind the quota project explicitly. Some Vertex AI endpoints
                # (Veo specifically) reject token requests when quota project
                # is ambiguous, surfacing as "invalid_scope: Invalid OAuth
                # scope or ID token audience provided."
                credentials = credentials.with_quota_project(_VERTEX_PROJECT)

                # Validate the token at startup so we surface auth issues
                # here in the worker logs instead of inside the model call.
                from google.auth.transport.requests import Request as _AuthReq
                try:
                    credentials.refresh(_AuthReq())
                    print(f"[VERTEX] token refresh OK; valid={credentials.valid} "
                          f"expiry={credentials.expiry}")
                except Exception as refresh_err:
                    print(f"[VERTEX] token refresh FAILED: {refresh_err}")

                client_kwargs["credentials"] = credentials
                print(f"[VERTEX] using explicit service account credentials "
                      f"({credentials.service_account_email}, "
                      f"quota_project={_VERTEX_PROJECT})")
            except Exception as e:
                print(f"[VERTEX] failed to load explicit credentials ({e}); "
                      f"falling back to ADC discovery")

        _genai_client = genai.Client(**client_kwargs)
    return _genai_client

# Combinatorial prompt system — elements combine to create unique prompts.
# 22 scenes x 12 palettes x 10 cameras x 8 conditions = 21,120 combinations
_BG_SCENES = [
    "calm ocean waves on a sandy beach",
    "northern lights aurora over a mountain lake",
    "abstract colorful smoke swirling slowly",
    "sunset clouds forming and dissolving",
    "underwater light rays through deep blue ocean",
    "tropical coral reef with colorful fish",
    "rolling fog over a green mountain valley",
    "lavender field stretching to the horizon",
    "gentle rain falling on a still lake",
    "desert sand dunes with wind patterns",
    "autumn leaves falling in a forest",
    "snow falling gently over pine trees",
    "bioluminescent waves crashing on dark shore",
    "cherry blossom petals floating in the wind",
    "crystal clear river flowing over smooth rocks",
    "volcanic lava flowing slowly into the ocean",
    "stars and milky way rotating over a landscape",
    "tropical waterfall cascading into a lagoon",
    "wildflowers swaying in a meadow breeze",
    "icebergs floating in arctic blue water",
    "hot air balloon shadows over green countryside",
    "lightning illuminating storm clouds from within",
]

_BG_PALETTES = [
    "golden hour warm tones",
    "cool blue and teal tones",
    "vibrant pink and purple sunset",
    "soft pastel colors",
    "deep navy and silver moonlight",
    "warm amber and orange",
    "vivid turquoise and coral",
    "moody indigo and violet",
    "bright green and emerald",
    "rose gold and blush pink",
    "fiery red and orange",
    "icy blue and white",
]

_BG_CAMERAS = [
    "slow aerial drone flyover",
    "smooth dolly forward movement",
    "gentle sideways tracking shot",
    "slow upward crane shot",
    "steady wide angle static shot with subtle movement",
    "slow orbit around the scene",
    "smooth descending aerial shot",
    "gentle push-in zoom",
    "slow parallax movement",
    "steady first-person glide forward",
]

_BG_CONDITIONS = [
    "cinematic depth of field",
    "soft natural lighting",
    "volumetric light rays",
    "misty atmospheric haze",
    "crystal clear vivid detail",
    "dreamy soft focus bokeh",
    "dramatic rim lighting",
    "ethereal glow",
]

_USED_PROMPTS_FILE = os.path.join(ASSETS_DIR, ".used_prompts.json")


_GENRE_SCENE_GUIDE = {
    "rock": (
        "Urban industrial streets, neon-lit alleyways, gritty rain on asphalt, "
        "smoke rising past dim streetlamps, electric storms over a dark city, "
        "abandoned warehouse interiors with shafts of light, distorted blurred "
        "headlights, raw concrete textures."
    ),
    "pop": (
        "Vibrant colorful neon lights, disco reflections, glittering city "
        "nightlife, abstract liquid color, mirrored prisms, geometric light "
        "patterns, energetic confetti motion, glossy gradient skies."
    ),
    "ballad": (
        "Soft sunset over calm ocean, slow drifting clouds, warm golden light "
        "through trees, candlelight macro, gentle rain on a window, "
        "single rose-gold reflections, pastel mountain mist."
    ),
    "latin": (
        "Tropical beach at golden hour, palm trees swaying, vibrant flower "
        "fields, salsa-club neon reds and yellows, sunlit Caribbean water, "
        "colorful murals motion-blurred, festive lantern strings."
    ),
    "reggaeton": (
        "Night cityscape with red and pink neon, palm-lined boulevards, "
        "luxury car reflections, abstract gold dust, velvet-textured colors, "
        "club laser patterns, vibrant rooftop lights."
    ),
    "hiphop": (
        "City skyline at night with gold accents, abstract luxury textures, "
        "marble and gold reflections, smoke-filled spotlights, rain on dark "
        "limousine paint, urban rooftop with skyline below."
    ),
    "electronic": (
        "Abstract glowing geometry, particle storms, fractal liquid metal, "
        "deep space nebulas, laser grid landscapes, holographic surfaces, "
        "cymatic patterns in colored ink."
    ),
    "indie": (
        "Misty forest at dawn, quiet vintage interiors with warm lamps, "
        "open road through autumn leaves, lone lighthouse on a cliff, soft "
        "film grain, dreamy lake reflections, hand-held cinematic frames."
    ),
    "folk": (
        "Mountain vistas at golden hour, dusty roads with sun flares, fields "
        "of wheat moving in wind, riverside campfire glow, weathered wood "
        "textures, sun rays through forest canopies."
    ),
    "metal": (
        "Volcanic landscapes with lava streams, dark cathedral interiors, "
        "stormy thunderclouds with lightning, cracked obsidian textures, "
        "burning pyres at dusk, abandoned iron mills."
    ),
}


def _normalize_genre(g: str) -> str:
    """Map free-text or UI selection to a key in _GENRE_SCENE_GUIDE."""
    if not g:
        return ""
    g = g.strip().lower()
    aliases = {
        "rock/punk": "rock", "punk": "rock", "alt rock": "rock",
        "pop/dance": "pop", "dance": "pop", "edm": "electronic",
        "house": "electronic", "techno": "electronic",
        "ballad/romantic": "ballad", "romantic": "ballad", "balada": "ballad",
        "latin/reggaeton": "latin", "latino": "latin", "salsa": "latin",
        "cumbia": "latin", "bachata": "latin",
        "hip hop": "hiphop", "hip-hop": "hiphop", "rap": "hiphop", "trap": "hiphop",
        "indie rock": "indie", "alternative": "indie",
    }
    if g in aliases:
        return aliases[g]
    if g in _GENRE_SCENE_GUIDE:
        return g
    return ""


# Concept selector — operator-controlled visual category for the background.
# When set, this hard-overrides the genre's scene vocabulary and forces
# Gemini's prompt into the chosen category. UMG asked for it because the
# genre alone wasn't tight enough — different songs in the same genre
# need different visual registers (a Karol G ballad vs a Karol G party
# anthem are both "latin" but should not look the same).
#
# Each value is the English vocabulary Gemini will pick from. Order in
# the catalogue matches the UI dropdown order.
_CONCEPT_SCENE_GUIDE = {
    "naturaleza":   "natural outdoor landscapes — dense forests, mountain valleys, rolling hills, open fields, rivers, sunsets over horizons",
    "tropical":     "tropical scenes — palm trees, caribbean beaches, vibrant flowers, festive lanterns, sunlit turquoise water, lush jungle",
    "acuatico":     "water-centric scenes — underwater light rays, rain on glass and pavement, deep ocean, slow-motion water droplets, flowing rivers",
    "ciudad":       "city skylines — modern downtowns, skyscrapers at golden hour, aerial cityscapes, glass facades, bridges, observation decks",
    "urbano":       "gritty urban — narrow alleys, neon-lit rain-slicked streets, graffiti walls, rooftops, fire escapes, smoking vents, industrial corners",
    "industrial":   "industrial environments — factories, exposed pipes, machinery, decaying warehouses, steel beams, smokestacks, foundries",
    "abstracto":    "abstract visuals — flowing geometric shapes, fractal patterns, particle clouds, color gradients, liquid metal, kaleidoscopic motion",
    "cosmico":      "cosmic scenes — spiral galaxies, star fields, colorful nebulas, planetary surfaces, deep space, comets, supernovae",
    "atmosferico":  "atmospheric mood — drifting smoke, dense fog, volumetric light rays, dust motes, soft haze, ethereal glow",
    "romantico":    "romantic mood — warm sunsets, candlelight, scattered rose petals, soft fabric textures, calm beaches at dusk, fireplace embers",
    "vintage":      "vintage / retro — Super 8 film grain, sepia tones, faded photographs, retro patterns, analog noise, old-paper textures",
    "cinematic":    "cinematic dramatic — chiaroscuro lighting, film-noir contrast, dramatic shadows, anamorphic lens flares, moody atmosphere",
    "club":         "club / dance scene — laser beams, smoke machines, neon strips, disco balls, strobe lights, dancefloor energy (no people, no faces)",
    "lujo":         "luxury aesthetics — polished marble, gold accents, crystal facets, high-gloss surfaces, fashion textures, jewelry close-ups",
    "minimalista":  "minimalist design — clean geometric shapes, smooth gradients, solid color planes, single-subject compositions, negative space",
}


# Movement-style hints injected into the Gemini system prompt's Hard-Rules
# section. UMG referenced 3 distinct registers in their meeting; we
# surface 4 explicit options (plus Auto) so the operator can pick the
# right "feel" per song. The genre + concept selectors decide WHAT the
# scene is; this decides HOW it moves.
_MOVEMENT_STYLE_RULES = {
    "sutil":         "Movement: minimal and ambient — gentle sway, slow drift, breathing motion. Subjects barely move. Easy to loop seamlessly.",
    "estandar":      "",  # no extra rule; the existing prompt template controls motion
    "foto-parallax": "Aesthetic: photographic still with subtle parallax — composition feels like a single photo, motion is restricted to slow camera moves, depth-of-field shifts, and lighting passes. No moving subjects.",
    "animado":       "Aesthetic: stylised 2D animated illustration — flat shapes, deliberate cartoon-like motion. NOT photorealistic.",
}


def _normalize_movement_style(s: str) -> str:
    """Map free-text or UI selection to a key in _MOVEMENT_STYLE_RULES.
    Returns "" for empty / unknown — caller treats that as Auto."""
    if not s:
        return ""
    s = s.strip().lower()
    aliases = {
        "subtle": "sutil", "minimal": "sutil", "minimo": "sutil",
        "standard": "estandar", "default": "estandar",
        "photo": "foto-parallax", "parallax": "foto-parallax",
        "foto+parallax": "foto-parallax", "foto_parallax": "foto-parallax",
        "animated": "animado", "illustration": "animado", "cartoon": "animado",
    }
    if s in aliases:
        return aliases[s]
    if s in _MOVEMENT_STYLE_RULES:
        return s
    return ""


def _normalize_concept(c: str) -> str:
    """Map free-text or UI selection to a key in _CONCEPT_SCENE_GUIDE."""
    if not c:
        return ""
    c = c.strip().lower()
    # Common alternate spellings (operator might tab in raw or with accents).
    aliases = {
        "nature": "naturaleza", "natural": "naturaleza",
        "city": "ciudad", "downtown": "ciudad", "skyline": "ciudad",
        "urban": "urbano", "street": "urbano", "alley": "urbano",
        "tropical/beach": "tropical", "playa": "tropical", "beach": "tropical",
        "water": "acuatico", "agua": "acuatico", "underwater": "acuatico",
        "abstract": "abstracto", "geometric": "abstracto",
        "cosmic": "cosmico", "space": "cosmico", "galaxy": "cosmico",
        "atmospheric": "atmosferico", "smoke": "atmosferico", "fog": "atmosferico",
        "romantic": "romantico", "love": "romantico",
        "vintage/retro": "vintage", "retro": "vintage",
        "cinematic/film": "cinematic", "film noir": "cinematic", "noir": "cinematic",
        "club/dance": "club", "rave": "club", "neon": "club",
        "luxury": "lujo", "premium": "lujo", "fashion": "lujo",
        "minimalist": "minimalista", "minimal": "minimalista",
        "industrial/factory": "industrial", "factory": "industrial",
    }
    if c in aliases:
        return aliases[c]
    if c in _CONCEPT_SCENE_GUIDE:
        return c
    return ""


def _analyze_lyrics_for_background(lyrics_text: str, artist: str, job_id: str = None,
                                    song_title: str = "", genre: str = "",
                                    concept: str = "",
                                    movement_style: str = "",
                                    match_lyrics: bool = True,
                                    background_hint: str | None = None) -> dict:
    """Use Gemini to analyze lyrics and choose visual style + prompt.

    match_lyrics=True  ("Inspirado en la letra"): lyrics anchor or infuse the scene.
    match_lyrics=False: concept/genre vocabulary only, lyrics are ignored.
    background_hint: optional free-form text from the operator (set by /edit)
      describing what they want the new background to convey. Overrides
      Gemini's default interpretation when present.

    Returns dict with:
      - style: "video" | "photo" | "illustration"
      - prompt: the generation prompt for Veo 3 or Imagen 4
    """
    from google import genai
    from provenance import record_ai_call

    client = _get_genai_client()

    normalized_genre = _normalize_genre(genre)
    normalized_concept = _normalize_concept(concept)
    normalized_movement = _normalize_movement_style(movement_style)
    movement_rule = _MOVEMENT_STYLE_RULES.get(normalized_movement, "")
    movement_extra_line = f"\n- {movement_rule}" if movement_rule else ""

    _PROMPT_RULES = (
        "- \"style\" must always be \"video\"\n"
        "- \"prompt\" is 80-120 words. Describe: (1) specific scene subject and setting "
        "in detail, (2) exact camera movement and framing, (3) color palette and dominant "
        "tones, (4) lighting type and direction, (5) atmosphere, mood, and at least one "
        "specific texture or material detail. Be precise and cinematic — avoid vague "
        "adjectives like \"beautiful\" or \"amazing\".\n"
        "- Pick a DIFFERENT specific scene each time (don't repeat across songs)\n"
        "- Never include people, faces, hands, or readable text in the scene"
    )
    # 3 contrastive examples (rock/urban, romantic ballad, acoustic) + an
    # explicit "do not copy verbatim" disclaimer. Replaces the prior
    # single example which biased Gemini toward "neon-lit rain-slicked
    # streets" output whenever concept/genre came empty (prompt-bleed
    # observed in prod 2026-05-12 on Rata Blanca "Mujer Amante" — a
    # ballad that got rendered as an industrial alley). Plus a hard guard
    # rail for ballads / love songs at the bottom.
    _BASE_INSTRUCTIONS = """Respond ONLY with a JSON object, no other text.

Output JSON shape — do NOT copy any of these example scenes verbatim;
they show only the format and the breadth of valid visual registers:

Example for rock / urban / gritty track:
{"style":"video","prompt":"Slow tracking shot through neon-lit rain-slicked streets, deep blue and red reflections, smoke rising past streetlamps, gritty cinematic 4k"}

Example for romantic ballad / love song:
{"style":"video","prompt":"Slow drift through a sunlit room at golden hour, warm light streaming through gauze curtains, soft focus on a glass catching the light, dust motes floating in the warm beam, intimate and calm, cinematic 4k"}

Example for introspective acoustic / folk track:
{"style":"video","prompt":"Slow aerial pull-back over a misty mountain valley at dawn, layers of soft blue and pink sky, distant silhouettes of pine trees, gentle wind moving low fog, contemplative and vast, cinematic 4k"}

GENRE-TONE COHERENCE (critical):
If the lyrics or declared genre suggest a love song, romantic ballad,
soft rock, acoustic, intimate or emotional theme, DO NOT default to
industrial, urban, dystopian, sewer, alleyway, or neon-rain backgrounds.
Bias toward warm interiors, golden-hour light, natural landscapes
(sunset, ocean, mountains at dusk), or symbolic intimate imagery (a
window, a candle, a glass catching light, hands intertwined). Industrial
alleys, neon streets, smoke, and rain are reserved for rock / metal /
punk / hip-hop tracks where the genre or lyrics anchor that vocabulary
explicitly. When in doubt, prefer warm/natural over urban/industrial."""
    # Keep _EXAMPLE pointing to the new block so existing f-strings below
    # absorb the change without further edits.
    _EXAMPLE = _BASE_INSTRUCTIONS

    if normalized_concept:
        concept_guide = _CONCEPT_SCENE_GUIDE[normalized_concept]
        genre_hint = (f"\n\nFor stylistic colour-grading flavour only "
                      f"(NOT for scene choice), the song genre is: "
                      f"{normalized_genre.upper()}.") if normalized_genre else ""

        if match_lyrics:
            # "Inspirado en la letra": concept sets the visual register,
            # lyrics theme infuses the specific execution within it.
            system_prompt = f"""{_EXAMPLE}

The operator has chosen the visual register: {normalized_concept.upper()}.
The scene MUST stay within this concept's visual vocabulary:
{concept_guide}{genre_hint}

STEP 0 — Read the lyrics and identify the SOUL of the song: its core theme, emotion, or story (e.g., football passion, longing for home, celebration, heartbreak, nature, freedom).

STEP 1 — Build a scene that:
- Is firmly within the {normalized_concept.upper()} visual vocabulary (non-negotiable)
- Expresses the song's theme through specific choices of color, shape, motion, and composition
- Examples:
  · ABSTRACTO + football → dynamic circular forms in green/white with kinetic energy
  · COSMICO + heartbreak → cold distant nebula, muted purples, slow lonely drift
  · NATURALEZA + summer joy → golden sun-drenched meadow, warm swaying grass, long shadows

Hard rules:
{_PROMPT_RULES}
- The concept vocabulary is the hard boundary — never exit it{movement_extra_line}"""
        else:
            # Strict concept mode: operator's visual choice, no lyrics influence.
            system_prompt = f"""{_EXAMPLE}

The operator has explicitly requested a {normalized_concept.upper()} background.

You MUST pick a scene that fits this concept's visual vocabulary:
{concept_guide}{genre_hint}

Hard rules:
{_PROMPT_RULES}
- The concept choice is binding — do NOT drift to a different visual category{movement_extra_line}"""

    elif normalized_genre:
        scene_guide = _GENRE_SCENE_GUIDE[normalized_genre]

        if match_lyrics:
            # "Inspirado en la letra": lyrics anchor the scene, genre styles it.
            system_prompt = f"""{_EXAMPLE}

The song genre is: {normalized_genre.upper()}

STEP 0 — Read the lyrics and identify the PRIMARY VISUAL SUBJECT: the concrete setting, object, or action the song is literally about (e.g., a football/soccer match, the ocean, a city at night, a road, a dance floor, rain, a forest). This is your FIRST input for scene choice.

STEP 1 — Choose the scene:
- If the lyrics have a CLEAR visual subject → build the scene around that subject. Apply the {normalized_genre.upper()} genre's color palette, lighting, and atmosphere to STYLE it — but the SCENE must reflect what the song is literally about.
- If the lyrics are abstract or purely emotional with no specific visual subject → fall back to this genre's visual vocabulary:
{scene_guide}

Hard rules:
{_PROMPT_RULES}
- If lyrics reference a sport (football, basketball, etc.) → use field/pitch/arena/equipment, NOT cars or generic cityscapes
- Do NOT default to "calm ocean at sunset" unless this song is BALLAD{movement_extra_line}"""
        else:
            # Strict genre mode: pick from genre vocabulary, ignore lyrics.
            system_prompt = f"""{_EXAMPLE}

The song genre is: {normalized_genre.upper()}

You MUST pick a scene from this genre's visual vocabulary:
{scene_guide}

Hard rules:
{_PROMPT_RULES}
- Do NOT default to "calm ocean at sunset" unless this song is BALLAD{movement_extra_line}"""

    else:
        if match_lyrics:
            # "Inspirado en la letra" + auto: lyrics anchor the scene,
            # genre classification controls color/mood only.
            system_prompt = f"""{_EXAMPLE}

STEP 0 — Read the lyrics and identify the PRIMARY VISUAL SUBJECT: the concrete setting, object, or action the song is literally about (e.g., a football/soccer match, the ocean, a city at night, a road trip, a dance floor, rain, a forest). This is your FIRST input for scene choice.

STEP 1 — Choose the scene:
- If the lyrics have a CLEAR visual subject → build the scene around that subject. Then classify genre (rock/pop/ballad/latin/reggaeton/hiphop/electronic/indie/folk/metal) to determine the COLOR PALETTE, LIGHTING, and ATMOSPHERE only — not the scene itself.
- If the lyrics are abstract or purely emotional with no specific visual subject → classify genre, then pick from the genre's vocabulary:
  - rock     → urban industrial streets, neon alleyways, gritty rain, electric storms, abandoned warehouses
  - pop      → vibrant neon, disco reflections, geometric light patterns, glossy gradient skies
  - ballad   → soft sunset, calm ocean, drifting clouds, warm golden light, candlelight
  - latin    → tropical beaches, palm trees, vibrant flowers, festive lanterns, sunlit caribbean water
  - reggaeton → night cityscape with red/pink neon, abstract color bursts, club laser patterns
  - hiphop   → city skyline at night with gold, marble luxury textures, smoke-filled spotlights
  - electronic → abstract geometry, particle storms, fractal liquid metal, laser grids
  - indie    → misty forests, vintage interiors, autumn roads, lone lighthouses, dreamy lakes
  - folk     → mountain vistas, dusty roads, wheat fields, riverside campfires
  - metal    → volcanic lava streams, dark cathedrals, stormy lightning, cracked obsidian

STEP 2 — Output JSON with an 80-120 word prompt. Describe: (1) specific scene subject and setting in detail, (2) exact camera movement and framing, (3) color palette and dominant tones, (4) lighting type and direction, (5) atmosphere, mood, and at least one specific texture or material detail. Be precise and cinematic — avoid vague adjectives like "beautiful" or "amazing".

Hard rules:
- "style" must always be "video"
- Pick a DIFFERENT specific scene each time (don't repeat across songs)
- If lyrics reference a sport (football, basketball, etc.) → use field/pitch/arena/equipment, NOT cars or generic cityscapes
- Do NOT default to "calm ocean at sunset" unless the song is genuinely BALLAD
- Never include people, faces, hands, or readable text in the scene"""
        else:
            # Strict auto mode: classify genre, pick vocabulary, no lyrics.
            system_prompt = f"""{_EXAMPLE}

Step 1: Classify the song's genre using the artist, title, and lyrics. Pick ONE of:
  rock, pop, ballad, latin, reggaeton, hiphop, electronic, indie, folk, metal

Step 2: Pick a scene from the matching genre's visual vocabulary:
- rock     → urban industrial streets, neon alleyways, gritty rain, electric storms, abandoned warehouses
- pop      → vibrant neon, disco reflections, geometric light patterns, glossy gradient skies
- ballad   → soft sunset, calm ocean, drifting clouds, warm golden light, candlelight
- latin    → tropical beaches, palm trees, vibrant flowers, festive lanterns, sunlit caribbean water
- reggaeton → night cityscape with red/pink neon, abstract color bursts, club laser patterns
- hiphop   → city skyline at night with gold, marble luxury textures, smoke-filled spotlights
- electronic → abstract geometry, particle storms, fractal liquid metal, laser grids
- indie    → misty forests, vintage interiors, autumn roads, lone lighthouses, dreamy lakes
- folk     → mountain vistas, dusty roads, wheat fields, riverside campfires
- metal    → volcanic lava streams, dark cathedrals, stormy lightning, cracked obsidian

Step 3: Output JSON with the chosen scene as an 80-120 word prompt.

Hard rules:
- "style" must always be "video"
- Pick a DIFFERENT specific scene each time (don't repeat across songs)
- Do NOT default to "calm ocean at sunset" unless the song is genuinely BALLAD
- Never include people, faces, hands, or readable text in the scene"""
        if movement_rule:
            system_prompt = system_prompt + "\n- " + movement_rule

    lyrics_sample = lyrics_text[:600] if lyrics_text else ""
    # Data minimization (UMG Guideline 14): optionally anonymize artist name
    _send_artist = os.environ.get("SEND_ARTIST_TO_AI", "true").lower() == "true"
    artist_label = artist if _send_artist else "the artist"
    title_part = f"\nSong title: {song_title}" if song_title else ""
    genre_part = f"\nDeclared genre: {normalized_genre}" if normalized_genre else ""
    concept_part = f"\nDeclared concept: {normalized_concept}" if normalized_concept else ""
    # Operator hint (set by /edit when the user clicked "Regenerar fondo"
    # and typed a free-form description of what they want). Sits at the
    # TOP of user_content with a strong header so Gemini treats it as the
    # dominant signal — overriding genre/concept/lyrics defaults that
    # caused off-tone backgrounds the operator already rejected.
    hint_block = ""
    if background_hint:
        hint_block = (
            f"[OPERATOR OVERRIDE — HIGHEST PRIORITY]\n"
            f"The operator was unhappy with previous backgrounds for this song "
            f"and wants the new one to convey: {background_hint.strip()}\n"
            f"Build the visual scene around this hint. This overrides the "
            f"default interpretation of genre/concept/lyrics — the operator's "
            f"explicit guidance wins. Stay coherent with the song's emotional "
            f"tone, but the IMAGERY must follow the hint.\n\n"
        )
    user_content = (
        f"{hint_block}"
        f"Artist: {artist_label}{title_part}{genre_part}{concept_part}\n\n"
        f"Lyrics (may be incomplete or noisy):\n"
        f"{lyrics_sample or '[transcription failed; rely on artist + title + declared metadata]'}"
    )
    full_prompt = f"system:{system_prompt}\nuser:{user_content}"

    recorder = record_ai_call(
        job_id=job_id or "unknown",
        step="lyrics_analysis",
        tool_name="gemini-2.5-flash",
        tool_provider="google_vertex",
        prompt=full_prompt,
        input_data_types=["artist_name", "lyrics_text_600chars"],
    ) if job_id else None

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=user_content,
            config=genai.types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=0.8,
                max_output_tokens=500,
                thinking_config=genai.types.ThinkingConfig(thinking_budget=0),
            ),
        )
        text = response.text.strip()
        print(f"[BG] Gemini raw: {text[:300]}")

        # Parse JSON from response (handles ```json blocks, multiline JSON)
        import re
        json_match = re.search(r'\{.*?\}', text, re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group())
                style = data.get("style", "video")
                prompt = data.get("prompt", "")
                if style not in ("video", "photo", "illustration"):
                    style = "video"
                if prompt and len(prompt) > 15:
                    print(f"[BG] Gemini chose: style={style}, prompt={prompt[:80]}...")
                    if recorder:
                        recorder.finish(response_summary=text[:500])
                    return {"style": style, "prompt": prompt}
            except json.JSONDecodeError:
                pass

        print("[BG] Failed to parse Gemini JSON, using combinatorial fallback")
        if recorder:
            recorder.finish(response_summary=f"parse_failed: {text[:200]}")
        return {"style": "video", "prompt": None}

    except Exception as e:
        print(f"[BG] Gemini analysis failed: {e}, using video fallback")
        if recorder:
            recorder.finish(response_summary=f"error: {str(e)[:200]}")
        return {"style": "video", "prompt": None}


def _get_unique_prompt(lyrics_text: str = None, artist: str = "", job_id: str = None,
                       song_title: str = "", genre: str = "", concept: str = "",
                       movement_style: str = "", match_lyrics: bool = True,
                       background_hint: str | None = None) -> dict:
    """Get a unique style+prompt combination. Returns {style, prompt}.

    Note: the local _USED_PROMPTS_FILE only sees this worker's previous
    prompts — Railway containers have ephemeral disk, so dedup across
    workers / restarts is best-effort. The Veo cache key downstream
    includes artist+title so even a duplicated Gemini prompt produces a
    fresh background per song (see `_generate_veo_video`).
    """
    used: list[str] = []
    if os.path.exists(_USED_PROMPTS_FILE):
        try:
            with open(_USED_PROMPTS_FILE) as f:
                used = json.load(f)
        except (json.JSONDecodeError, OSError):
            used = []

    # Gemini analysis
    if lyrics_text or song_title:
        result = _analyze_lyrics_for_background(
            lyrics_text or "", artist, job_id=job_id, song_title=song_title,
            genre=genre, concept=concept, movement_style=movement_style,
            match_lyrics=match_lyrics, background_hint=background_hint,
        )
        if result["prompt"] and result["prompt"] not in used:
            used.append(result["prompt"])
            try:
                with open(_USED_PROMPTS_FILE, "w") as f:
                    json.dump(used, f)
            except OSError:
                pass
            return result

    # Fallback: combinatorial video prompt
    for _ in range(50):
        scene = random.choice(_BG_SCENES)
        palette = random.choice(_BG_PALETTES)
        camera = random.choice(_BG_CAMERAS)
        condition = random.choice(_BG_CONDITIONS)
        prompt = f"{camera} of {scene}, {palette}, {condition}, 4k, photorealistic"
        if prompt not in used:
            used.append(prompt)
            try:
                with open(_USED_PROMPTS_FILE, "w") as f:
                    json.dump(used, f)
            except OSError:
                pass
            return {"style": "video", "prompt": prompt}

    return {
        "style": "video",
        "prompt": f"{random.choice(_BG_CAMERAS)} of {random.choice(_BG_SCENES)}, {random.choice(_BG_PALETTES)}, {random.choice(_BG_CONDITIONS)}, 4k, photorealistic",
    }


_last_veo_request = 0  # timestamp of last Veo API call
_VEO_COOLDOWN = 5      # seconds between Veo requests (Veo 3.1 has 50 req/min quota)


def _veo_access_token() -> str:
    """Build an explicit cloud-platform-scoped access token for the Vertex AI
    REST API. Bypasses google-genai SDK's internal auth chain which has been
    triggering invalid_scope errors on Railway despite the credentials being
    valid (Gemini works on the same token; only Veo rejects through the SDK)."""
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request

    creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if not creds_path or not os.path.exists(creds_path):
        raise RuntimeError(f"GOOGLE_APPLICATION_CREDENTIALS not found: {creds_path!r}")
    creds = service_account.Credentials.from_service_account_file(
        creds_path,
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    creds = creds.with_quota_project(_VERTEX_PROJECT)
    creds.refresh(Request())
    return creds.token


def _veo_cache_key(prompt: str, model: str, params: dict) -> str:
    """Stable hash of the Veo request. Two requests with the same prompt and
    parameters return the same key, so we can dedupe paid generations across
    runs (especially during testing — UMG production prompts are unique per
    song so cache hits are rare there)."""
    import hashlib as _hash
    import json as _json
    payload = _json.dumps(
        {"prompt": prompt, "model": model, "params": params},
        sort_keys=True,
        separators=(",", ":"),
    )
    return _hash.sha256(payload.encode()).hexdigest()[:16]


def _generate_veo_video(prompt: str, output_path: str, job_id: str = None,
                        cache_namespace: str = "",
                        image_path: str | None = None,
                        movement_style: str = "") -> str:
    """Generate a video clip with Google Veo 3 via direct Vertex AI REST API.

    We bypass google-genai SDK for Veo specifically because its internal auth
    chain hits "invalid_scope: Invalid OAuth scope or ID token audience" on
    Railway even when our explicit credentials work for Gemini through the
    same SDK. Direct REST gives us full control over headers, scopes, and
    endpoints.

    Endpoint: predictLongRunning -> poll operation -> download mp4.
    Rate-limit aware (5 attempts with exponential backoff).
    R2-cached by prompt hash so identical retries do not bill twice.

    `image_path`: optional path to a JPG/PNG. When provided, the request is
    sent in image-to-video mode (Veo 3.1 supports a base64-encoded `image`
    field on `instances[0]`). The user's image is animated according to the
    prompt while preserving its identity. Defaults to None (text-to-video).

    `movement_style`: when set to "animado", the safe-prompt suffix drops
    the "no CGI / no animation" clauses so they don't contradict the
    cartoon-illustration aesthetic. All other safety clauses (no people,
    no text, etc.) stay in place.
    """
    from provenance import record_ai_call
    import storage as _storage
    import time as _time
    import requests as _req
    global _last_veo_request

    if movement_style == "animado":
        # Cartoon / 2D illustration aesthetic — keep all safety clauses
        # except the "no CGI / no animation" pair, which would directly
        # contradict the requested look. Other prohibitions (no text, no
        # people, no logos, etc.) stay in place.
        safe_prompt = (
            f"{prompt}. Stylised 2D animated illustration, flat shapes, "
            "deliberate cartoon-like motion. "
            "No text, no words, no letters, no signs, no billboards, no posters, "
            "no banners, no graffiti, no shop windows, no street signs, no neon "
            "signs, no logos, no trademarks, no brand symbols, no people, "
            "no faces, no hands."
        )
    else:
        safe_prompt = (
            f"{prompt}. Photorealistic, filmed with cinema camera, real footage. "
            "No text, no words, no letters, no signs, no billboards, no posters, "
            "no banners, no graffiti, no shop windows, no street signs, no neon "
            "signs, no logos, no trademarks, no brand symbols, no people, "
            "no faces, no hands, no CGI, no animation."
        )

    # veo-3.1-fast at $0.10/s (no audio) is 75% cheaper than the standard
    # veo-3.1-generate at $0.40/s. Visual quality is slightly softer; we
    # apply a small gaussian blur after generation to smooth edges and
    # improve lyric legibility on top of the background.
    #
    # Blur sigma was 2.0 originally — UMG flagged the rendered backgrounds
    # as low-definition during the live demo, and the heavy blur was the
    # main culprit (compounding the softness Veo Fast already has). Now
    # 1.0 by default — preserves more detail while still smoothing micro
    # artefacts. Tune via env var without redeploy if needed.
    model = os.environ.get("VEO_MODEL", "veo-3.1-fast-generate-001").strip()
    veo_params = {
        "aspectRatio": "16:9",
        "sampleCount": 1,
        "generateAudio": False,
    }
    try:
        blur_sigma = float(os.environ.get("BG_BLUR_SIGMA", "1.0"))
    except ValueError:
        blur_sigma = 1.0

    # Cache key includes a per-song namespace (artist|title) so two different
    # songs that happen to receive the same Gemini prompt — common when
    # transcription degrades and Gemini falls back to a generic "ocean
    # sunset" template — still generate independent Veo backgrounds.
    # Without this, all problem-songs ended up sharing one cached video
    # because the cache key was prompt-only.
    cache_params = {**veo_params, "blur_sigma": blur_sigma, "ns": cache_namespace or ""}
    cache_key_hash = _veo_cache_key(safe_prompt, model, cache_params)
    cache_object_key = f"cache/veo/{cache_key_hash}.mp4"

    recorder = record_ai_call(
        job_id=job_id or "unknown",
        step="video_bg",
        tool_name=model,
        tool_provider="google_vertex",
        prompt=safe_prompt,
        input_data_types=["generated_prompt"],
    ) if job_id else None

    if _storage.is_enabled() and _storage.object_exists(cache_object_key):
        if _storage.download_object(cache_object_key, output_path):
            size_mb = os.path.getsize(output_path) / 1024 / 1024
            print(f"[BG] Veo cache HIT ({cache_key_hash}): {size_mb:.1f} MB — skipped paid generation")
            if recorder:
                recorder.finish(
                    response_summary=f"cache_hit: {size_mb:.1f}MB key={cache_key_hash}",
                    output_artifact=output_path,
                )
            return output_path

    elapsed = _time.time() - _last_veo_request
    if elapsed < _VEO_COOLDOWN and _last_veo_request > 0:
        wait = _VEO_COOLDOWN - elapsed
        print(f"[BG] Cooldown: waiting {wait:.0f}s before next Veo request...")
        _time.sleep(wait)

    base_url = (
        f"https://{_VERTEX_LOCATION}-aiplatform.googleapis.com/v1"
        f"/projects/{_VERTEX_PROJECT}/locations/{_VERTEX_LOCATION}"
        f"/publishers/google/models/{model}"
    )
    submit_url = f"{base_url}:predictLongRunning"

    # Build the instance dict. When the operator supplied an image AND
    # marked "animar con AI", attach it as base64 — Veo 3.1 then animates
    # the image while honoring the prompt instead of generating from
    # scratch. Worker logs this so we can monitor success rate.
    instance: dict = {"prompt": safe_prompt}
    if image_path and os.path.isfile(image_path):
        try:
            import base64 as _b64
            with open(image_path, "rb") as _img:
                img_bytes = _img.read()
            ext = os.path.splitext(image_path)[1].lower()
            mime = "image/png" if ext == ".png" else "image/jpeg"
            instance["image"] = {
                "bytesBase64Encoded": _b64.b64encode(img_bytes).decode("ascii"),
                "mimeType": mime,
            }
            print(f"[BG] image-to-video Veo call with user image "
                  f"({len(img_bytes)} bytes, {mime})")
        except OSError as e:
            print(f"[BG] failed to read image_path {image_path}: {e}; "
                  f"falling back to text-to-video")

    request_body = {
        "instances": [instance],
        "parameters": veo_params,
    }

    # Retry policy
    # ------------
    # The previous loop conflated two failure modes (HTTP 429 and arbitrary
    # exceptions) under the same `for/else: raise "rate limit exceeded"` and
    # could fall through to the polling stage with operation_name=None on a
    # transient request error. We now:
    #   1. Track success/last-error explicitly so the exit reason is honest.
    #   2. Cap backoff at 120 s (was 60 × 5 = 300 s, exceeding the worker
    #      timeout under stress).
    #   3. Distinguish 429/RESOURCE_EXHAUSTED ("rate-limited") from network
    #      errors ("transient") so the surfaced error message is accurate.
    MAX_BACKOFF_S = 120
    MAX_ATTEMPTS = 5
    operation_name: str | None = None
    last_error: str | None = None
    rate_limit_hits = 0

    for attempt in range(MAX_ATTEMPTS):
        try:
            print(f"[BG] Veo 3: generating video (attempt {attempt + 1}/{MAX_ATTEMPTS})...")
            token = _veo_access_token()
            r = _req.post(
                submit_url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "x-goog-user-project": _VERTEX_PROJECT,
                },
                json=request_body,
                timeout=60,
            )
            if r.status_code == 429 or "RESOURCE_EXHAUSTED" in r.text:
                rate_limit_hits += 1
                last_error = f"HTTP {r.status_code} rate-limited"
                # Capped exponential backoff + ±20 % jitter. Without
                # jitter, N concurrent jobs that all hit a 429 at the
                # same instant retry in lock-step → second wave of
                # 429s → cascade. Jitter spreads the retry window so
                # quota recovers naturally.
                base = min(MAX_BACKOFF_S, 30 * (2 ** attempt))
                wait = base * random.uniform(0.8, 1.2)
                print(f"[BG] Rate limited (HTTP {r.status_code}), waiting {wait:.1f}s before retry...")
                _time.sleep(wait)
                continue
            if not r.ok:
                detail = r.text[:500]
                # Non-retryable: bubble immediately so the caller can mark
                # the job error with a useful reason.
                raise RuntimeError(
                    f"Veo predictLongRunning HTTP {r.status_code}: {detail}"
                )
            payload = r.json()
            operation_name = payload.get("name")
            if not operation_name:
                raise RuntimeError(f"Veo response missing 'name': {payload}")
            break
        except RuntimeError:
            raise
        except Exception as e:
            last_error = f"network/transient: {e}"
            print(f"[BG] Veo 3 attempt {attempt + 1} request error: {e}")
            base = min(MAX_BACKOFF_S, 15 * (2 ** attempt))
            wait = base * random.uniform(0.8, 1.2)
            _time.sleep(wait)
            continue

    if operation_name is None:
        reason = last_error or "unknown"
        summary = (
            f"error: rate_limited_after_{MAX_ATTEMPTS}_retries"
            if rate_limit_hits == MAX_ATTEMPTS
            else f"error: {reason} after {MAX_ATTEMPTS} retries"
        )
        if recorder:
            recorder.finish(response_summary=summary)
        if rate_limit_hits == MAX_ATTEMPTS:
            raise RuntimeError(f"Veo 3 rate limit exceeded after {MAX_ATTEMPTS} retries")
        raise RuntimeError(f"Veo 3 submission failed after {MAX_ATTEMPTS} retries: {reason}")

    _last_veo_request = _time.time()
    print(f"[BG] Veo 3 operation: {operation_name}")

    # Poll the operation. The REST endpoint mirrors the model URL prefix.
    poll_url = (
        f"https://{_VERTEX_LOCATION}-aiplatform.googleapis.com/v1/{operation_name}"
    )
    fetch_url = f"{base_url}:fetchPredictOperation"
    poll_deadline = _time.time() + 600
    op_payload: dict | None = None
    while True:
        if _time.time() > poll_deadline:
            raise TimeoutError("Veo 3 operation timed out after 10 min")
        _time.sleep(10)
        token = _veo_access_token()
        # Vertex's long-running publisher operations need the
        # fetchPredictOperation helper (a plain GET on the operation name
        # returns 404 for publisher models). Body carries the operation name.
        r = _req.post(
            fetch_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "x-goog-user-project": _VERTEX_PROJECT,
            },
            json={"operationName": operation_name},
            timeout=30,
        )
        if not r.ok:
            print(f"[BG] poll HTTP {r.status_code}: {r.text[:200]}; retrying...")
            continue
        op_payload = r.json()
        if op_payload.get("done"):
            break

    if "error" in op_payload:
        err = op_payload["error"]
        if recorder:
            recorder.finish(response_summary=f"error: {str(err)[:200]}")
        raise RuntimeError(f"Veo operation failed: {err}")

    response_data = op_payload.get("response", {})
    videos = response_data.get("videos") or response_data.get("generatedVideos") or []
    if not videos:
        if recorder:
            recorder.finish(response_summary=f"error: no videos in response: {response_data}")
        raise RuntimeError(f"Veo response had no videos: {response_data}")

    video_entry = videos[0]
    # Field name varies between API versions: gcsUri / videoUri / video.uri
    video_uri = (
        video_entry.get("gcsUri")
        or video_entry.get("videoUri")
        or (video_entry.get("video") or {}).get("uri")
    )
    bytes_b64 = video_entry.get("bytesBase64Encoded") or (
        video_entry.get("video") or {}
    ).get("bytesBase64Encoded")

    if bytes_b64:
        # Inline bytes — decode and write directly.
        import base64 as _b64
        with open(output_path, "wb") as f:
            f.write(_b64.b64decode(bytes_b64))
    elif video_uri:
        token = _veo_access_token()
        dl = _req.get(
            video_uri,
            headers={"Authorization": f"Bearer {token}"},
            timeout=120,
        )
        dl.raise_for_status()
        with open(output_path, "wb") as f:
            f.write(dl.content)
    else:
        if recorder:
            recorder.finish(response_summary=f"error: video has no uri/bytes: {video_entry}")
        raise RuntimeError(f"Veo video has no uri or bytes: {video_entry}")

    size_mb = os.path.getsize(output_path) / 1024 / 1024
    print(f"[BG] Veo 3 video saved: {size_mb:.1f} MB (raw)")

    # Apply subtle gaussian blur. Veo Fast outputs are slightly softer than
    # standard; a small blur normalises that softness, hides minor artefacts,
    # and improves contrast for the lyric overlay rendered on top.
    import subprocess as _sp
    blurred = output_path + ".blurred.mp4"
    try:
        _sp.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", output_path,
                "-vf", f"gblur=sigma={blur_sigma}",
                "-c:a", "copy",
                blurred,
            ],
            check=True,
            timeout=60,
        )
        os.replace(blurred, output_path)
        size_mb = os.path.getsize(output_path) / 1024 / 1024
        print(f"[BG] Blur applied (sigma={blur_sigma}): {size_mb:.1f} MB")
    except Exception as e:
        print(f"[BG] Blur skipped (non-fatal): {e}")
        if os.path.exists(blurred):
            try:
                os.unlink(blurred)
            except OSError:
                pass

    if _storage.is_enabled():
        try:
            _storage.upload_file(output_path, cache_object_key)
            print(f"[BG] Veo cache STORED: {cache_object_key}")
        except Exception as e:
            print(f"[BG] Veo cache upload failed (non-fatal): {e}")

    if recorder:
        recorder.finish(
            response_summary=f"video_generated: {size_mb:.1f}MB key={cache_key_hash}",
            output_artifact=output_path,
        )
    return output_path


def _generate_imagen_image(prompt: str, output_path: str, max_retries: int = 5,
                            job_id: str = None, model: str | None = None) -> str:
    """Generate an image with Google Imagen 4. Auto-retries on rate limit.

    `model` lets the caller override the default. Library generation can
    pass `imagen-4.0-ultra-generate-001` for marquee-quality stills;
    runtime job rendering keeps the standard tier for cost reasons.
    """
    from google import genai
    from google.genai.errors import ClientError
    from provenance import record_ai_call
    import time as _time

    client = _get_genai_client()

    chosen_model = (model
                    or os.environ.get("IMAGEN_MODEL")
                    or "imagen-4.0-generate-001").strip()

    safe_prompt = f"{prompt}. No text, no words, no letters, no people, no faces, no hands."

    recorder = record_ai_call(
        job_id=job_id or "unknown",
        step="image_bg",
        tool_name=chosen_model,
        tool_provider="google_vertex",
        prompt=safe_prompt,
        input_data_types=["generated_prompt"],
    ) if job_id else None

    for attempt in range(max_retries):
        try:
            print(f"[BG] {chosen_model}: generating image (attempt {attempt + 1})...")
            response = client.models.generate_images(
                model=chosen_model,
                prompt=safe_prompt,
                config=genai.types.GenerateImagesConfig(
                    number_of_images=1,
                    aspect_ratio="16:9",
                ),
            )
            break
        except ClientError as e:
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                wait = 60 * (attempt + 1)
                print(f"[BG] Rate limited, waiting {wait}s before retry...")
                _time.sleep(wait)
            else:
                if recorder:
                    recorder.finish(response_summary=f"error: {str(e)[:200]}")
                raise
    else:
        if recorder:
            recorder.finish(response_summary="error: rate_limit_exceeded")
        raise RuntimeError("Imagen 4 rate limit exceeded after all retries")

    image = response.generated_images[0]
    # Save image bytes
    img_bytes = image.image.image_bytes
    with open(output_path, "wb") as f:
        f.write(img_bytes)

    size_kb = os.path.getsize(output_path) / 1024
    print(f"[BG] Imagen 4 saved: {size_kb:.0f} KB")
    if recorder:
        recorder.finish(
            response_summary=f"image_generated: {size_kb:.0f}KB",
            output_artifact=output_path,
        )
    return output_path


def _extract_frame_from_video(video_path: str, output_image_path: str) -> str:
    """Extract a representative still frame from a video and save it as PNG.

    Used by the "library variation" flow: we pick a frame from the
    user-selected library video and pass it to Veo as image-to-video
    seed so Veo derives a new clip visually similar to the original.

    The chosen timestamp is the middle of the clip — the first second
    is often a fade-in / black frame and the last second a fade-out, so
    the middle is the most representative single frame.

    Raises RuntimeError if ffprobe/ffmpeg is unavailable or the file is
    not a readable video.
    """
    try:
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                video_path,
            ],
            capture_output=True, text=True, timeout=30,
        )
        duration = float((probe.stdout or "0").strip() or 0.0)
    except (subprocess.SubprocessError, ValueError, FileNotFoundError) as e:
        raise RuntimeError(f"ffprobe failed on {video_path}: {e}") from e
    timestamp = max(0.0, duration / 2.0) if duration > 0 else 0.0

    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{timestamp:.3f}",
        "-i", video_path,
        "-frames:v", "1",
        "-vf", "scale='min(1920,iw)':-2",
        output_image_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0 or not os.path.exists(output_image_path):
        raise RuntimeError(
            f"ffmpeg frame extraction failed (rc={result.returncode}): {result.stderr[:300]}"
        )
    return output_image_path


def _score_video_relevance(video_path: str, prompt: str) -> int:
    """Ask Gemini Vision whether the video matches the intended scene prompt.

    Extracts one frame and returns a relevance score 1-10.
    Fails open (returns 8) so a Gemini error never blocks a good video.
    """
    from google import genai
    import tempfile

    tmp_frame = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            tmp_frame = f.name
        _extract_frame_from_video(video_path, tmp_frame)

        client = _get_genai_client()
        with open(tmp_frame, "rb") as f:
            image_bytes = f.read()

        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[
                genai.types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                (
                    f"This is a frame from an AI-generated background video.\n"
                    f"Intended scene: \"{prompt}\"\n\n"
                    f"Score how well the frame matches the intended scene, 1-10.\n"
                    f"Focus on whether the MAIN SUBJECT is correct "
                    f"(e.g. if the scene should show a football pitch but shows cars, score 1-2).\n"
                    f"Respond with ONLY a single integer, nothing else."
                ),
            ],
            config=genai.types.GenerateContentConfig(
                temperature=0.0,
                max_output_tokens=5,
                thinking_config=genai.types.ThinkingConfig(thinking_budget=0),
            ),
        )
        import re as _re
        m = _re.search(r'\b(10|[1-9])\b', response.text)
        score = int(m.group()) if m else 5
        return max(1, min(10, score))
    except Exception as e:
        print(f"[BG] Relevance score error (fail-open): {e}")
        return 8
    finally:
        if tmp_frame:
            try:
                os.unlink(tmp_frame)
            except OSError:
                pass


def _ensure_background(style_hint: str, job_dir: str, lyrics_text: str = None,
                       artist: str = "", job_id: str = None,
                       song_title: str = "", genre: str = "",
                       concept: str = "",
                       movement_style: str = "",
                       image_to_video_path: str | None = None,
                       match_lyrics: bool = True,
                       background_hint: str | None = None) -> str:
    """Generate background using AI. Gemini picks the best style for the song.

    background_hint: optional free-form operator description, set via /edit
    when the user clicks "Regenerar fondo" and types what they want. Flows
    into Gemini's user_content as a [OPERATOR OVERRIDE] block.

    Returns path to .mp4 (video style) or .jpg/.png (photo/illustration style).
    """
    # If there are video files in backgrounds dir, use those instead
    all_videos = []
    if os.path.isdir(BACKGROUNDS_DIR):
        for root, _, files in os.walk(BACKGROUNDS_DIR):
            all_videos.extend(f for f in files if f.lower().endswith(".mp4"))
    if all_videos:
        return None

    # Generate video background with Veo 3 (always video, no images)
    result = _get_unique_prompt(
        lyrics_text, artist, job_id=job_id, song_title=song_title, genre=genre,
        concept=concept, movement_style=movement_style, match_lyrics=match_lyrics,
        background_hint=background_hint,
    )
    prompt = result["prompt"]

    bg_path = os.path.join(job_dir, "bg_generated.mp4")
    import time as _time_bg
    quality_retry_used = False
    for attempt in range(3):
        try:
            _generate_veo_video(
                prompt, bg_path, job_id=job_id,
                cache_namespace=f"{artist}|{song_title}",
                image_path=image_to_video_path,
                movement_style=movement_style,
            )
            # Semantic relevance check — always score, but cap retries at one
            # to bound cost (+$0.80 worst case). quality_retry_used gates the
            # re-generation decision, not the scoring itself, so the retry's
            # result is also evaluated before we accept and return it.
            score = _score_video_relevance(bg_path, prompt)
            print(f"[BG] Relevance score: {score}/10 for prompt: {prompt[:60]}...")
            if score < 7 and not quality_retry_used:
                quality_retry_used = True
                print(f"[BG] Score {score} < 7 — generating new prompt and retrying VEO")
                result = _get_unique_prompt(
                    lyrics_text, artist, job_id=job_id, song_title=song_title,
                    genre=genre, concept=concept, movement_style=movement_style,
                    match_lyrics=match_lyrics,
                )
                prompt = result["prompt"]
                continue
            if score < 7:
                print(f"[BG] Score {score} < 7 after retry — accepting best available result")
            return bg_path
        except Exception as e:
            print(f"[BG] Veo 3 attempt {attempt + 1}/3 failed: {e}")
            if attempt < 2:
                wait = 30 * (attempt + 1)
                print(f"[BG] Waiting {wait}s before retry...")
                _time_bg.sleep(wait)

    # All Veo attempts failed — render a gradient as fallback.
    # We do NOT fall back to a library asset: UMG and other rights-sensitive
    # tenants need clear provenance of every visual element, and a stock asset
    # silently substituted into an AI-mode job would break that contract.
    print("[BG] Veo 3 unavailable, falling back to gradient background")
    fallback_path = os.path.join(job_dir, "bg_gradient_fallback.mp4")
    gradient = _make_gradient_clip(30.0, style_hint)
    gradient.write_videofile(fallback_path, fps=24, logger=None)
    gradient.close()
    return fallback_path


def _ken_burns_clip(image_path: str, duration: float, spec: RenderSpec | None = None):
    """Create an animated Ken Burns clip with periodic direction changes."""
    if spec is None:
        spec = RenderSpec.youtube_default()
    img = np.array(Image.open(image_path))
    h, w = img.shape[:2]

    # Each cycle lasts ~12 seconds, with a different random direction
    cycle_dur = 12.0
    num_cycles = max(1, int(math.ceil(duration / cycle_dur)))

    # Pre-generate random directions for each cycle
    random.seed(None)
    cycles = []
    for _ in range(num_cycles):
        cycles.append({
            "zoom_in": random.choice([True, False]),
            "pan_x": random.uniform(-0.08, 0.08),
            "pan_y": random.uniform(-0.05, 0.05),
        })

    def make_frame(t):
        idx = min(int(t / cycle_dur), num_cycles - 1)
        c = cycles[idx]
        progress = (t - idx * cycle_dur) / cycle_dur

        # Smooth ease in/out within each cycle
        progress = 0.5 - 0.5 * math.cos(progress * math.pi)

        if c["zoom_in"]:
            scale = 1.0 + 0.25 * progress
        else:
            scale = 1.25 - 0.25 * progress

        cw = int(w / scale)
        ch = int(h / scale)
        cx = int((w - cw) / 2 + c["pan_x"] * progress * w)
        cy = int((h - ch) / 2 + c["pan_y"] * progress * h)
        cx = max(0, min(cx, w - cw))
        cy = max(0, min(cy, h - ch))

        crop = img[cy:cy + ch, cx:cx + cw]
        resized = np.array(
            Image.fromarray(crop).resize((spec.width, spec.height), Image.LANCZOS)
        )
        return resized

    return VideoClip(make_frame, duration=duration).set_fps(spec.fps)


# ---------------------------------------------------------------------------
# Step 2 — Full HD lyric video
# ---------------------------------------------------------------------------

_USED_BACKGROUNDS_FILE = os.path.join(ASSETS_DIR, ".used_backgrounds.json")


def _find_background_video(exclude: list[str] | None = None) -> str | None:
    """Pick a random background video without repeating until all are used.

    `exclude` is a per-call blacklist of paths to skip (used by the
    content-validation retry loop to avoid re-picking a background that
    just failed policy in this same job).
    """
    exclude = exclude or []
    all_videos: list[str] = []
    if os.path.isdir(BACKGROUNDS_DIR):
        for root, _, files in os.walk(BACKGROUNDS_DIR):
            all_videos.extend(
                os.path.join(root, f)
                for f in files if f.lower().endswith(".mp4")
            )

    if not all_videos:
        return None

    # Load history of used videos
    used: list[str] = []
    if os.path.exists(_USED_BACKGROUNDS_FILE):
        try:
            with open(_USED_BACKGROUNDS_FILE) as f:
                used = json.load(f)
        except (json.JSONDecodeError, OSError):
            used = []

    # Filter out already used + per-call exclusions; if nothing left, reset
    # the cycle but keep honouring the exclusion list (we still don't want
    # to re-pick a background that already failed validation this job).
    available = [v for v in all_videos if v not in used and v not in exclude]
    if not available:
        print(f"[BG] All {len(all_videos)} backgrounds used, resetting cycle")
        used = []
        available = [v for v in all_videos if v not in exclude]
    if not available:
        return None

    pick = random.choice(available)
    used.append(pick)

    # Save updated history
    try:
        with open(_USED_BACKGROUNDS_FILE, "w") as f:
            json.dump(used, f)
    except OSError:
        pass

    print(f"[BG] Selected: {os.path.basename(pick)} ({len(all_videos) - len(available)} of {len(all_videos)} used)")
    return pick


def _select_validated_background(job_id: str, max_attempts: int = 3) -> tuple[str | None, list[dict]]:
    """Pick a library background and validate it against UMG Guideline 15
    BEFORE the expensive render kicks in. If validation rejects, pick a
    different background and try again, up to `max_attempts`.

    Returns (chosen_path, all_rejection_issues). chosen_path is None if
    no clean background was found within the attempts budget.
    """
    from content_validator import validate_video, validate_image

    rejected: list[str] = []
    issues: list[dict] = []
    for attempt in range(1, max_attempts + 1):
        candidate = _find_background_video(exclude=rejected)
        if not candidate:
            break
        ext = os.path.splitext(candidate)[1].lower()
        validate_fn = (
            validate_video if ext in (".mp4", ".mov", ".webm") else validate_image
        )
        result = validate_fn(candidate, job_id=job_id)
        if result.get("passed"):
            print(
                f"[VALIDATION] bg accepted on attempt {attempt}: "
                f"{os.path.basename(candidate)}"
            )
            return candidate, issues
        print(
            f"[VALIDATION] bg rejected on attempt {attempt} "
            f"({os.path.basename(candidate)}): {result.get('issues')}"
        )
        for it in result.get("issues") or []:
            issues.append({"attempt": attempt, "bg": os.path.basename(candidate), **it})
        rejected.append(candidate)
    return None, issues


_GRADIENT_PALETTES = {
    "oscuro": [(10, 10, 30), (30, 15, 60), (80, 20, 80), (40, 10, 50)],
    "neon": [(10, 5, 40), (80, 0, 120), (0, 100, 130), (120, 0, 80)],
    "minimal": [(180, 180, 195), (200, 190, 210), (170, 180, 200), (210, 200, 195)],
    "calido": [(60, 20, 10), (140, 60, 15), (180, 90, 20), (100, 30, 10)],
}


def _make_gradient_clip(duration: float, style: str = "oscuro",
                        spec: RenderSpec | None = None):
    """Generate a cinematic animated gradient as fallback background."""
    if spec is None:
        spec = RenderSpec.youtube_default()
    palette = _GRADIENT_PALETTES.get(style, _GRADIENT_PALETTES["oscuro"])
    top = np.array(palette[0], dtype=np.float64)
    mid1 = np.array(palette[1], dtype=np.float64)
    mid2 = np.array(palette[2], dtype=np.float64)
    bot = np.array(palette[3], dtype=np.float64)

    _rows = np.zeros((spec.height, spec.width, 3), dtype=np.float64)
    for y in range(spec.height):
        ratio = y / spec.height
        if ratio < 0.33:
            color = top + (mid1 - top) * (ratio / 0.33)
        elif ratio < 0.66:
            color = mid1 + (mid2 - mid1) * ((ratio - 0.33) / 0.33)
        else:
            color = mid2 + (bot - mid2) * ((ratio - 0.66) / 0.34)
        _rows[y, :] = color

    def _gradient_frame(t):
        shift = 20 * np.sin(t * 0.12)
        shift2 = 12 * np.cos(t * 0.08)
        frame = _rows.copy()
        frame[:, :, 0] = np.clip(frame[:, :, 0] + shift, 0, 255)
        frame[:, :, 1] = np.clip(frame[:, :, 1] + shift2 * 0.5, 0, 255)
        frame[:, :, 2] = np.clip(frame[:, :, 2] - shift * 0.6, 0, 255)
        return frame.astype(np.uint8)

    return VideoClip(_gradient_frame, duration=duration).set_fps(spec.fps)


def _cover_resize(clip, target_w=1920, target_h=1080):
    """Resize and crop a video clip to cover target_w x target_h (CSS cover)."""
    src_w, src_h = clip.size
    # Scale so the smallest dimension fills the target
    scale = max(target_w / src_w, target_h / src_h)
    new_w = int(math.ceil(src_w * scale))
    new_h = int(math.ceil(src_h * scale))
    resized = clip.resize((new_w, new_h))
    # Center crop to exact target size
    x_offset = (new_w - target_w) // 2
    y_offset = (new_h - target_h) // 2
    return resized.crop(x1=x_offset, y1=y_offset, width=target_w, height=target_h)


def _prerender_looped_bg(bg_path: str, duration: float, job_dir: str,
                         target_w=1920, target_h=1080,
                         out_name: str = "bg_looped.mp4") -> str:
    """Pre-render a seamlessly looped background using palindrome (A + reverse(A)).

    A straight -stream_loop jumps from the last frame back to the first, which
    is visible as a "pop" when the scene has camera movement. Concatenating A
    with its reverse makes the last frame of one pass match the first frame of
    the next — the loop is mathematically seamless.

    We scale and crop first, then palindrome, then loop the palindrome to fill
    the requested duration.
    """
    out_path = os.path.join(job_dir, out_name)
    cmd = [
        "ffmpeg", "-y",
        "-stream_loop", "-1",
        "-i", bg_path,
        "-t", str(duration),
        "-filter_complex", (
            f"[0:v]scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
            f"crop={target_w}:{target_h},setpts=PTS-STARTPTS,split[a][b];"
            "[b]reverse[br];"
            "[a][br]concat=n=2:v=1:a=0"
        ),
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-an",
        out_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        # Fall back to the simple loop if the palindrome filter graph fails
        # (e.g. clip too short or memory-constrained machines).
        print(f"[BG] Palindrome loop failed, falling back to stream_loop: "
              f"{result.stderr[-200:]}")
        cmd_fallback = [
            "ffmpeg", "-y",
            "-stream_loop", "-1",
            "-i", bg_path,
            "-t", str(duration),
            "-vf", (
                f"scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
                f"crop={target_w}:{target_h},"
                "setpts=PTS-STARTPTS"
            ),
            "-c:v", "libx264", "-preset", "fast", "-crf", "20",
            "-pix_fmt", "yuv420p", "-an",
            out_path,
        ]
        result = subprocess.run(cmd_fallback, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg loop failed: {result.stderr[-300:]}")

    size_mb = os.path.getsize(out_path) / 1024 / 1024
    print(f"[BG] Pre-rendered palindrome loop: {duration:.0f}s, {size_mb:.1f} MB")
    return out_path


def _attach_close_chain(target_clip, owned_clips):
    """Make `target_clip.close()` also close `owned_clips`.

    moviepy's resize/crop/concatenate return new clips that retain refs
    to their source clips, but their .close() does NOT cascade to the
    sources. Long-running workers leaked an ffmpeg subprocess + FD per
    background load until this helper was introduced. Use only on the
    very clip the caller will eventually close, with the sources whose
    lifetime should match it.
    """
    original_close = target_clip.close
    def chained_close():
        try:
            original_close()
        finally:
            for c in owned_clips:
                try:
                    c.close()
                except Exception:
                    pass
    target_clip.close = chained_close
    return target_clip


def _get_background_clip_from_path(bg_path: str, style: str, duration: float,
                                   job_dir: str = None, spec: RenderSpec | None = None):
    """Load a background video, loop it seamlessly via ffmpeg, return clip.

    Always returns a single VideoFileClip whose lifetime is owned by the
    caller (the caller is expected to .close() it when the composition
    is finished). When the source already covers the requested duration
    we return a derived clip with its source attached via
    _attach_close_chain so the caller's close cascades. When the source
    is shorter, we pre-render a single seamless loop file via ffmpeg —
    the previous "concatenate N opened clips" fallback leaked one
    VideoFileClip per loop iteration.
    """
    if spec is None:
        spec = RenderSpec.youtube_default()
    try:
        clip = VideoFileClip(bg_path)
        clip.get_frame(0)
        clip_dur = clip.duration
        clip.close()
    except Exception as e:
        raise RuntimeError(f"Cannot load background video: {e}")

    if clip_dur >= duration:
        # Open ONCE, derive subclip + cover-resize, attach the source so
        # the caller's eventual close() releases the underlying ffmpeg
        # reader.
        src = VideoFileClip(bg_path)
        derived = _cover_resize(src.subclip(0, duration), spec.width, spec.height)
        return _attach_close_chain(derived, [src])

    # Always pre-render a single seamless loop file. The job_dir-supplied
    # path was already clean; the no-job_dir fallback used to concatenate
    # N opened VideoFileClips and leak each one because moviepy's
    # concatenate_videoclips does NOT cascade-close its inputs.
    if job_dir:
        looped_dir = job_dir
        cleanup_dir = None
    else:
        looped_dir = tempfile.mkdtemp(prefix="genly_bg_loop_")
        cleanup_dir = looped_dir
    looped_name = f"bg_looped_{spec.width}x{spec.height}.mp4"
    looped_path = _prerender_looped_bg(
        bg_path, duration, looped_dir,
        target_w=spec.width, target_h=spec.height,
        out_name=looped_name,
    )
    looped_clip = VideoFileClip(looped_path)

    if cleanup_dir is not None:
        # Sweep the temp dir when the caller closes the clip — no other
        # process should be reading the looped file by then.
        def _rmtree_safely():
            try:
                if os.path.exists(looped_path):
                    os.unlink(looped_path)
            except OSError:
                pass
            try:
                os.rmdir(cleanup_dir)
            except OSError:
                pass
        _orig_close = looped_clip.close
        def _close_with_cleanup():
            try:
                _orig_close()
            finally:
                _rmtree_safely()
        looped_clip.close = _close_with_cleanup
    return looped_clip


# Font pool — Google Fonts only (SIL OFL = full commercial use, no royalties)
# Fonts: prefer the bundled copy inside the backend (so the Docker image
# self-contains them and Railway's build context — which is just lyricgen/backend
# — has them available). Fall back to the repo-level /assets/fonts for local
# dev runs that haven't moved the files yet.
_FONTS_DIR_CANDIDATES = [
    os.path.join(os.path.dirname(__file__), "fonts"),
    os.path.join(os.path.dirname(__file__), "..", "assets", "fonts"),
]
_FONTS_DIR = next(
    (p for p in _FONTS_DIR_CANDIDATES if os.path.isdir(p)),
    _FONTS_DIR_CANDIDATES[0],
)
_LYRIC_FONTS = [
    # Sans-serif (clean, modern)
    "Montserrat-Bold.ttf",
    "Montserrat-ExtraBold.ttf",
    "Poppins-Bold.ttf",
    "Outfit-Bold.ttf",       # Gilroy alternative
    "Roboto-Bold.ttf",
    # Display (impactful, bold)
    "BebasNeue-Regular.ttf",
    "Oswald-Bold.ttf",
    "Anton-Regular.ttf",
]
_FONT_POOL = [
    os.path.join(_FONTS_DIR, f)
    for f in _LYRIC_FONTS
    if os.path.isfile(os.path.join(_FONTS_DIR, f))
] if os.path.isdir(_FONTS_DIR) else []


# Public-facing font catalogue. The frontend picker mirrors this list and
# renders previews in the browser via the Google Fonts CDN — every entry's
# `google_family` + `google_weight` matches the local TTF in `filename`,
# so what the operator sees in the dropdown is what the worker renders.
# UMG asked for these eight typefaces specifically; Futura and Gilroy are
# proprietary (Adobe/HypeForType) so we surface their closest libre
# substitutes (Jost / Outfit) and label the option honestly so the
# operator knows it's a stylistic match, not the licensed face.
_FONT_CATALOGUE = [
    {"id": "jost-bold",        "filename": "Jost-Bold.ttf",            "label": "Jost (estilo Futura)",     "google_family": "Jost",        "google_weight": 700},
    {"id": "montserrat-bold",  "filename": "Montserrat-Bold.ttf",      "label": "Montserrat",                "google_family": "Montserrat",  "google_weight": 700},
    {"id": "poppins-bold",     "filename": "Poppins-Bold.ttf",         "label": "Poppins",                   "google_family": "Poppins",     "google_weight": 700},
    {"id": "outfit-bold",      "filename": "Outfit-Bold.ttf",          "label": "Outfit (estilo Gilroy)",   "google_family": "Outfit",      "google_weight": 700},
    {"id": "roboto-bold",      "filename": "Roboto-Bold.ttf",          "label": "Roboto",                    "google_family": "Roboto",      "google_weight": 700},
    {"id": "bebas-neue",       "filename": "BebasNeue-Regular.ttf",    "label": "Bebas Neue",                "google_family": "Bebas Neue",  "google_weight": 400},
    {"id": "oswald-bold",      "filename": "Oswald-Bold.ttf",          "label": "Oswald",                    "google_family": "Oswald",      "google_weight": 700},
    {"id": "anton",            "filename": "Anton-Regular.ttf",        "label": "Anton",                     "google_family": "Anton",       "google_weight": 400},
]


def _resolve_font(font_id: str) -> str | None:
    """Map a public font id to a real path under _FONTS_DIR. Empty string
    or unknown id → None, signaling the caller to use the random pool
    (existing "Auto" behavior). Never raises; never returns a missing
    path."""
    if not font_id:
        return None
    for entry in _FONT_CATALOGUE:
        if entry["id"] == font_id:
            path = os.path.join(_FONTS_DIR, entry["filename"])
            return path if os.path.isfile(path) else None
    return None


def _apply_case(text: str, case: str) -> str:
    """Apply text-case transformation matching the user's choice."""
    if case == "upper":
        return text.upper()
    if case == "title":
        return text.title()
    if case == "lower":
        return text.lower()
    return text  # "original" — keep as transcribed


def _text_position_func(spec, motion: str, seg_duration: float,
                        clip_x: int = 0, clip_y: int = 0,
                        shadow_offset: int = 0):
    """Return a position callable (or string/tuple) for a text clip.

    clip_x / clip_y are the top-left pixel coordinates that would place
    the clip centered on screen (computed by the caller from actual clip
    dimensions). shadow_offset shifts both axes so the shadow sits just
    behind the main text. Motion values: "none" | "subtle" | "float".

    NOTE: "float" is temporarily aliased to "subtle". The per-frame
    position callable forces moviepy's CompositeVideoClip to evaluate
    `pos(t)` for every frame of every text layer, blocking the
    optimizations that let static positions cache. For songs with 30+
    lyric lines × 4320 frames (3 min @ 24fps) that's an order of
    magnitude slower and was hitting the 20-min RQ timeout on prod.
    Re-enable the distinct "float" amplitude once we move the text
    layer rendering to ffmpeg overlay filters (where per-frame motion
    is essentially free).
    """
    import math
    if motion == "float":
        motion = "subtle"
    if motion == "none" or not motion:
        if shadow_offset:
            return (clip_x + shadow_offset, clip_y + shadow_offset)
        return "center"

    period = max(seg_duration, 0.5)
    amp_scale = spec.text_scale

    if motion == "subtle":
        amplitude = max(2, int(round(4 * amp_scale)))

        def pos(t):
            dy = amplitude * math.sin(2 * math.pi * t / period)
            return (clip_x + shadow_offset, clip_y + int(dy) + shadow_offset)
    else:  # "float"
        amp_y = max(4, int(round(8 * amp_scale)))
        amp_x = max(1, int(round(3 * amp_scale)))

        def pos(t):
            dy = amp_y * math.sin(2 * math.pi * t / period)
            dx = amp_x * math.sin(math.pi * t / period + 0.5)
            return (clip_x + int(dx) + shadow_offset, clip_y + int(dy) + shadow_offset)

    return pos


_CONTRAST_SETTINGS = {
    "subtle": {"stroke_mult": 1.5, "shadow_opacity": 0.40, "extra_shadow": False},
    "medium": {"stroke_mult": 2.5, "shadow_opacity": 0.55, "extra_shadow": False},
    "strong": {"stroke_mult": 3.5, "shadow_opacity": 0.65, "extra_shadow": True},
}


def _make_text_clip(
    text: str,
    seg_start: float,
    seg_end: float,
    font: str = "Arial",
    spec: RenderSpec | None = None,
    text_case: str = "upper",
    font_scale: float = 1.0,
    lyric_transition: str = "cut",
    text_motion: str = "none",
    text_contrast: str = "medium",
):
    """Create a clean text clip matching pro lyric video style (bold white, outline + shadow)."""
    import unicodedata
    if spec is None:
        spec = RenderSpec.youtube_default()

    # Apply case transform then sanitize for ImageMagick
    display_text = unicodedata.normalize("NFC", _apply_case(text, text_case))
    display_text = display_text.replace("@", "").replace("`", "'").replace("\x00", "")

    # Empty-string guard — ImageMagick errors with "label expected" on blank input
    if not display_text.strip():
        return []

    scale = spec.text_scale
    # font_scale is the user-chosen size multiplier (default 1.0 = unchanged)
    font_scale = max(0.6, min(1.5, float(font_scale or 1.0)))

    text_len = len(display_text)
    if text_len > 80:
        base_fontsize = int(round(55 * scale))
        text_width = int(round(1700 * scale))
    elif text_len > 50:
        base_fontsize = int(round(70 * scale))
        text_width = int(round(1650 * scale))
    else:
        base_fontsize = int(round(85 * scale))
        text_width = int(round(1500 * scale))

    fontsize = max(18, int(round(base_fontsize * font_scale)))

    shadow_offset = max(1, int(round(3 * scale)))
    fallback_font = os.path.join(_FONTS_DIR, "Montserrat-Bold.ttf")
    contrast = _CONTRAST_SETTINGS.get(text_contrast, _CONTRAST_SETTINGS["medium"])
    stroke_width = max(1.0, contrast["stroke_mult"] * scale)

    seg_duration = max(0.1, seg_end - seg_start)

    # Fade duration — capped at 1/3 of segment so short clips don't break
    _FADE_DURATIONS = {"fade": 0.15, "fade_slow": 0.30}
    fade_dur = _FADE_DURATIONS.get(lyric_transition, 0.0)
    fade_dur = min(fade_dur, seg_duration / 3)

    def _try_text_clip(txt, fsize, fnt, color, **kwargs):
        try:
            return TextClip(txt, fontsize=fsize, font=fnt, color=color,
                            method="caption", size=(text_width, None), align="center", **kwargs)
        except Exception:
            return TextClip(txt, fontsize=fsize, font=fallback_font, color=color,
                            method="caption", size=(text_width, None), align="center", **kwargs)

    shadow = _try_text_clip(display_text, fontsize, font, "black").set_opacity(contrast["shadow_opacity"])
    sh = shadow.size[1]
    # Centered top-left coordinates for a clip of size (text_width, sh)
    base_x = (spec.width - text_width) // 2
    base_y = (spec.height - sh) // 2
    shadow_pos = _text_position_func(spec, text_motion, seg_duration,
                                     clip_x=base_x, clip_y=base_y,
                                     shadow_offset=shadow_offset)
    if callable(shadow_pos):
        shadow = shadow.set_position(lambda t, _p=shadow_pos: _p(t))
    else:
        shadow = shadow.set_position((base_x + shadow_offset, base_y + shadow_offset))
    shadow = shadow.set_start(seg_start).set_end(seg_end)

    layers = []

    # "strong" mode: add a counter-shadow at the opposite offset to widen the halo
    if contrast["extra_shadow"]:
        shadow2 = _try_text_clip(display_text, fontsize, font, "black").set_opacity(contrast["shadow_opacity"] * 0.5)
        shadow2_pos = _text_position_func(spec, text_motion, seg_duration,
                                          clip_x=base_x, clip_y=base_y,
                                          shadow_offset=-shadow_offset)
        if callable(shadow2_pos):
            shadow2 = shadow2.set_position(lambda t, _p=shadow2_pos: _p(t))
        else:
            shadow2 = shadow2.set_position((base_x - shadow_offset, base_y - shadow_offset))
        shadow2 = shadow2.set_start(seg_start).set_end(seg_end)
        if fade_dur > 0:
            shadow2 = shadow2.crossfadein(fade_dur).crossfadeout(fade_dur)
        layers.append(shadow2)

    layers.append(shadow)

    txt_pos = _text_position_func(spec, text_motion, seg_duration,
                                  clip_x=base_x, clip_y=base_y,
                                  shadow_offset=0)
    txt = _try_text_clip(display_text, fontsize, font, "white",
                         stroke_color="black", stroke_width=stroke_width)
    if callable(txt_pos):
        txt = txt.set_position(lambda t, _p=txt_pos: _p(t))
    else:
        txt = txt.set_position("center")
    txt = txt.set_start(seg_start).set_end(seg_end)

    if fade_dur > 0:
        shadow = shadow.crossfadein(fade_dur).crossfadeout(fade_dur)
        txt = txt.crossfadein(fade_dur).crossfadeout(fade_dur)

    layers.append(txt)
    return layers


_UMG_PROFILE_NAMES = {
    3: {"HQ"},
    4: {"4444"},
    5: {"4444 XQ", "XQ"},
}


def _eval_fraction(value: str) -> float:
    """Evaluate a rational string like '24000/1001' into a float."""
    if value is None:
        return 0.0
    if "/" in value:
        num, den = value.split("/", 1)
        try:
            d = float(den)
            return float(num) / d if d else 0.0
        except ValueError:
            return 0.0
    try:
        return float(value)
    except ValueError:
        return 0.0


def _short_prores_spec(umg_spec: dict) -> "RenderSpec":
    """Build a vertical (1080×1920, 9:16) ProRes spec out of a UMG
    delivery dict. We keep the operator's chosen fps + prores_profile
    so the ProRes short stays consistent with the master, but flip
    dimensions and DAR for the short's vertical canvas.
    """
    from render_spec import UMG_PRORES_PROFILES
    profile_id = int(umg_spec.get("prores_profile", 3))
    fps_val = float(umg_spec.get("fps", 24.0))
    prof = UMG_PRORES_PROFILES.get(profile_id, UMG_PRORES_PROFILES[3])
    return RenderSpec(
        profile="umg",
        width=1080, height=1920,
        fps=fps_val,
        dar=(9, 16),
        codec="prores_ks",
        prores_profile=profile_id,
        pix_fmt=prof["pix_fmt"],
        audio_codec="pcm_s24le",
        color_primaries="bt709",
        container="mov",
    )


def _probe_dims_fps(path: str) -> tuple[int, int, str] | None:
    """ffprobe v:0 to extract (width, height, r_frame_rate). Returns None
    on any failure — caller falls back to the legacy scale+fps path so a
    flaky probe never breaks an otherwise-valid transcode."""
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,r_frame_rate",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            text=True, timeout=30,
        ).strip().splitlines()
        if len(out) < 3:
            return None
        return int(out[0]), int(out[1]), out[2]
    except Exception:
        return None


def _transcode_to_prores(input_path: str, mov_path: str,
                          spec: "RenderSpec",
                          timeout_sec: int = 600) -> None:
    """Transcode an h264 mp4 → ProRes .mov per the given RenderSpec.

    Used by /download/{id}/umg_master and /download/{id}/umg_short to
    produce ProRes deliverables lazily on the first download click,
    instead of running a second moviepy render at pipeline time.
    ffmpeg only — no moviepy involvement — so the moviepy palindrome-
    loop hang that breaks the dual-render path doesn't apply here.

    PURE RECODE FAST PATH (the world-class case for UMG):
    when the source MP4 is already at the exact target dimensions and
    fps (which is what `pipeline.run_pipeline` produces for any
    delivery_profile in (umg, both) since the world-class refactor),
    we skip the `scale=` and `fps=` filters entirely. Frames pass
    through 1:1 — no chroma stretch, no fps interpolation. UMG's
    manual QC explicitly rejects frame-rate-conversion artifacts, so
    this path is what makes the master shippable for any of the 4 ×
    8 frame-size × fps combinations they accept.

    LEGACY SCALE+FPS PATH:
    when the source dims/fps don't match (older jobs rendered before
    the refactor, or a custom upload route that bypasses the spec),
    fall back to the previous behaviour with `scale=lanczos` +
    `fps=`. Logs a warning because the output may fail UMG manual QC.

    Args:
      input_path: path to the existing source mp4 (lyric_video.mp4
                  or short.mp4).
      mov_path:   destination for the ProRes .mov.
      spec:       a RenderSpec — RenderSpec.umg(**umg_spec) for the
                  master, or _short_prores_spec(umg_spec) for the
                  vertical short.
      timeout_sec: hard kill after N seconds. 10 min is plenty for a
                   3-min song; longer means ffmpeg hung on a bad file.

    The output passes _validate_umg_master under normal conditions:
      - codec=prores_ks, profile per spec
      - exact width × height (no scale on fast path; lanczos on legacy)
      - fps via -r (rational for fractional fps); skipped on fast path
        so the bitstream timebase comes straight from the source
      - audio re-encoded to pcm_s24le @ 48 kHz @ 2 ch
      - bt709 color tags, progressive, mov container, DAR per spec.

    Raises RuntimeError on ffmpeg failure or post-transcode validation
    failure. The caller is responsible for cleaning up partial output.
    """
    src = _probe_dims_fps(input_path)
    pure_recode = (
        src is not None
        and src[0] == spec.width
        and src[1] == spec.height
        and src[2] == spec.fps_str
    )

    vf_chain = (
        # Fast path: SAR normalize + BT.709 metadata stamp. No scale,
        # no fps — frames go through 1:1.
        "setsar=1,setparams=colorspace=bt709:"
        "color_primaries=bt709:color_trc=bt709:range=tv"
        if pure_recode
        else
        # Legacy path: scale + fps conversion (may produce QC-failing
        # artifacts; logged below).
        f"scale={spec.width}:{spec.height}:flags=lanczos,"
        f"fps={spec.fps_str},setsar=1,"
        f"setparams=colorspace=bt709:color_primaries=bt709:"
        f"color_trc=bt709:range=tv"
    )

    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", input_path,
        "-vf", vf_chain,
    ]
    if not pure_recode:
        # Force the timebase only when we're converting fps; the fast
        # path inherits the source fps which is already exact.
        cmd += ["-r", spec.fps_str]
    cmd += [
        "-c:v", "prores_ks",
        "-profile:v", str(spec.prores_profile),
        "-pix_fmt", spec.pix_fmt,
        "-vendor", "apl0",
        "-color_primaries", "bt709",
        "-color_trc", "bt709",
        "-colorspace", "bt709",
        "-color_range", "tv",
        "-aspect", f"{spec.dar[0]}:{spec.dar[1]}",
        "-movflags", "+faststart+write_colr",
        # Audio: re-encode to UMG's required spec regardless of input.
        "-c:a", "pcm_s24le",
        "-ar", "48000",
        "-ac", "2",
        "-f", "mov",
        mov_path,
    ]

    if pure_recode:
        print(f"[PRORES] pure-recode {os.path.basename(input_path)} → "
              f"{os.path.basename(mov_path)} ({spec.width}×{spec.height} @ "
              f"{spec.fps_str}, profile {spec.prores_profile}) — "
              f"source dims+fps match target, no scale/fps filter.")
    else:
        src_desc = f"{src[0]}×{src[1]}@{src[2]}" if src else "unknown"
        print(f"[PRORES] LEGACY scale+fps {os.path.basename(input_path)} "
              f"({src_desc}) → {os.path.basename(mov_path)} "
              f"({spec.width}×{spec.height} @ {spec.fps_str}, "
              f"profile {spec.prores_profile}). "
              f"WARNING: source mismatch may produce frame-rate-"
              f"conversion artifacts that fail UMG manual QC.")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
    if result.returncode != 0:
        # Best-effort cleanup of a partial file before raising.
        try:
            if os.path.exists(mov_path):
                os.unlink(mov_path)
        except OSError:
            pass
        raise RuntimeError(
            f"ffmpeg ProRes transcode failed (rc={result.returncode}): "
            f"{result.stderr[-500:]}"
        )

    # Log what ffprobe sees on the freshly-encoded master for any future
    # debugging — colorspace problems are notoriously sticky on ProRes.
    try:
        _probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries",
             "stream=codec_name,profile,width,height,pix_fmt,"
             "color_space,color_primaries,color_transfer,color_range,"
             "field_order,display_aspect_ratio",
             "-of", "default=noprint_wrappers=1", mov_path],
            capture_output=True, text=True, timeout=30,
        )
        print("[PRORES] ffprobe stream fields:")
        for line in (_probe.stdout or "").strip().splitlines():
            print(f"  {line}")
    except Exception as _e:  # pragma: no cover
        print(f"[PRORES] ffprobe diagnostic failed: {_e}")

    errors = _validate_umg_master(mov_path, spec)
    if errors:
        # Print errors before deleting so the worker logs surface
        # what ffprobe reported vs. what we expected — the diagnostic
        # ffprobe dump above gives the actual values; this line gives
        # the validator's interpretation.
        print(f"[PRORES] validation failed: {errors}")
        try:
            os.unlink(mov_path)
        except OSError:
            pass
        raise RuntimeError(
            f"transcoded ProRes failed UMG validation: {'; '.join(errors)}"
        )

    size_mb = os.path.getsize(mov_path) / 1024 / 1024
    print(f"[PRORES] master ready: {size_mb:.1f} MB")


def _validate_umg_master(path: str, spec: RenderSpec) -> list[str]:
    """Run ffprobe on the master and return a list of spec violations.

    ffprobe doesn't surface `color_primaries` and `color_transfer` for ProRes
    output (the colr atom is written but not always parsed). We require
    `color_space == "bt709"` (reliable, comes from bitstream coefficients) and
    tolerate missing color_primaries / color_transfer.

    For fractional fps (23.976 / 29.97 / 59.94), we require exact rational
    match in `r_frame_rate` to catch decimal-vs-rational drift that UMG QC
    may flag. For integer fps a 0.01 tolerance is fine.
    """
    cmd = [
        "ffprobe", "-v", "error",
        "-print_format", "json",
        "-show_streams", "-show_format",
        path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return [f"ffprobe failed: {result.stderr[-200:]}"]

    try:
        probe = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        return [f"ffprobe output not JSON: {e}"]

    errors: list[str] = []
    v_streams = [s for s in probe.get("streams", []) if s.get("codec_type") == "video"]
    if not v_streams:
        return ["no video stream found"]
    v = v_streams[0]

    if v.get("codec_name") != "prores":
        errors.append(f"codec_name={v.get('codec_name')}, expected prores")
    expected_profiles = _UMG_PROFILE_NAMES.get(spec.prores_profile, set())
    if expected_profiles and v.get("profile") not in expected_profiles:
        errors.append(
            f"profile={v.get('profile')}, expected one of {expected_profiles}"
        )
    if (v.get("width"), v.get("height")) != (spec.width, spec.height):
        errors.append(
            f"dimensions={v.get('width')}x{v.get('height')}, "
            f"expected {spec.width}x{spec.height}"
        )

    # Frame rate: exact rational for fractional fps (R1); 0.01 tolerance for integer.
    actual_r_frame_rate = v.get("r_frame_rate")
    if spec.fps in FPS_RATIONAL:
        expected_rational = FPS_RATIONAL[spec.fps]
        if actual_r_frame_rate != expected_rational:
            errors.append(
                f"r_frame_rate={actual_r_frame_rate}, expected {expected_rational} "
                f"(exact rational required for fractional fps)"
            )
    else:
        actual_fps = _eval_fraction(actual_r_frame_rate)
        if abs(actual_fps - spec.fps) > 0.01:
            errors.append(
                f"r_frame_rate={actual_r_frame_rate} ({actual_fps:.3f}), "
                f"expected {spec.fps}"
            )

    if v.get("pix_fmt") != spec.pix_fmt:
        errors.append(f"pix_fmt={v.get('pix_fmt')}, expected {spec.pix_fmt}")

    # Color: only color_space is reliably surfaced for ProRes by ffprobe. The
    # colr atom (color_primaries + color_transfer) is written by ffmpeg but
    # ffprobe doesn't parse it for ProRes output across all versions. We
    # require color_space, and tolerate missing primaries/transfer.
    if v.get("color_space") != "bt709":
        errors.append(f"color_space={v.get('color_space')}, expected bt709")
    for optional_key in ("color_primaries", "color_transfer"):
        actual = v.get(optional_key)
        if actual is not None and actual != "bt709":
            errors.append(f"{optional_key}={actual}, expected bt709 (or absent)")

    # display_aspect_ratio is reported like "16:9" or "256:135"
    expected_dar = f"{spec.dar[0]}:{spec.dar[1]}"
    actual_dar = v.get("display_aspect_ratio")
    if actual_dar not in (expected_dar, None):
        # Tolerate equivalent ratios (e.g. "256:135" vs reduced form)
        if _eval_fraction(actual_dar.replace(":", "/")) and abs(
            _eval_fraction(actual_dar.replace(":", "/"))
            - spec.dar[0] / spec.dar[1]
        ) > 0.01:
            errors.append(
                f"display_aspect_ratio={actual_dar}, expected {expected_dar}"
            )
    field_order = v.get("field_order")
    if field_order not in (None, "progressive"):
        errors.append(f"field_order={field_order}, expected progressive")
    fmt = probe.get("format", {}).get("format_name", "")
    if "mov" not in fmt:
        errors.append(f"format_name={fmt}, expected a mov container")

    # Audio. UMG requires PCM 24-bit, 48 kHz, stereo. Catching this here
    # prevents the silent regression where moviepy's audio_fps default
    # (44100) overrides our ffmpeg `-ar 48000` and ships a non-compliant
    # master. Source MP3s are usually 44.1 kHz so this is a real risk.
    a_streams = [s for s in probe.get("streams", []) if s.get("codec_type") == "audio"]
    if not a_streams:
        errors.append("no audio stream found")
    else:
        a = a_streams[0]
        if a.get("codec_name") != "pcm_s24le":
            errors.append(
                f"audio codec_name={a.get('codec_name')}, expected pcm_s24le"
            )
        try:
            sample_rate = int(a.get("sample_rate", 0))
        except (TypeError, ValueError):
            sample_rate = 0
        if sample_rate != 48000:
            errors.append(
                f"audio sample_rate={sample_rate}, expected 48000"
            )
        if int(a.get("channels", 0)) != 2:
            errors.append(
                f"audio channels={a.get('channels')}, expected 2 (stereo)"
            )

    return errors


def generate_lyric_video(
    mp3_path: str,
    segments: list[dict],
    style: str,
    job_dir: str,
    artist: str,
    bg_image_path: str | None = None,
    spec: RenderSpec | None = None,
    font: str | None = None,
    song_title: str = "",
    text_case: str = "upper",
    font_scale: float = 1.0,
    lyric_transition: str = "cut",
    text_motion: str = "none",
    text_contrast: str = "medium",
) -> tuple[str, str, str | None]:
    """Generate a lyric video. Returns (video_path, font, bg_source).

    When `spec` is None, produces the YouTube MP4 (H.264 / 1080p / 24 fps /
    yuv420p). When `spec.profile == "umg"`, produces a ProRes .mov master
    with BT.709 color tags and display aspect ratio per UMG specs.

    `bg_source` is the path to the raw background (mp4 or jpg) so short/
    thumbnail can reuse it without burned-in lyrics.
    """
    if spec is None:
        spec = RenderSpec.youtube_default()

    audio = AudioFileClip(mp3_path)
    duration = audio.duration

    # Load background — can be video (.mp4) or image (.jpg/.png with Ken Burns)
    bg_source = bg_image_path
    if not bg_source:
        bg_source = _find_background_video()
    if not bg_source:
        raise RuntimeError("No background available. Check Veo 3 API or add videos to assets/backgrounds/")

    if bg_source.lower().endswith((".jpg", ".jpeg", ".png")):
        bg = _ken_burns_clip(bg_source, duration, spec=spec)
    else:
        bg = _get_background_clip_from_path(bg_source, style, duration, job_dir, spec=spec)

    # Pick a font for this job (or reuse the caller-provided one).
    # For UMG profile, the choice is deterministic (derived from job_dir hash)
    # so retries of the same job produce the same font — UMG QC and editorial
    # review don't expect font drift across re-deliveries.
    if font is None:
        if _FONT_POOL:
            if spec.profile == "umg":
                seed = int(hashlib.sha1(job_dir.encode()).hexdigest()[:8], 16)
                font = _FONT_POOL[seed % len(_FONT_POOL)]
            else:
                font = random.choice(_FONT_POOL)
        else:
            font = "Arial"
    print(f"[FONT] Selected: {os.path.basename(font)}")

    # Drop empty / whitespace-only segments BEFORE clamping so the
    # neighbor indices used for overlap clamp are correct. Operator
    # can leave blank rows from "Agregar línea" if they don't type
    # lyrics; passing empty text to ImageMagick triggers a "label
    # expected" error and aborts the whole render.
    if segments:
        before = len(segments)
        segments = [s for s in segments if (s.get("text") or "").strip()]
        dropped = before - len(segments)
        if dropped:
            print(f"[RENDER] dropped {dropped} blank segment(s) before render")

    # Defensive normalization — clamp each segment's end to the next
    # segment's start (with a 50ms gap) so two subtitles can never
    # render simultaneously. Operator-edited timestamps from sync mode
    # can leave end > next.start when lines were anchored closer than
    # the original duration. Frontend also clamps but we re-clamp here
    # in case other callers (batch CLI, API replays) bypass it.
    if segments:
        sorted_segs = sorted(segments, key=lambda s: s["start"])
        cleaned = []
        for i, seg in enumerate(sorted_segs):
            new_end = seg["end"]
            if i + 1 < len(sorted_segs):
                next_start = sorted_segs[i + 1]["start"]
                if new_end > next_start - 0.05:
                    new_end = max(seg["start"] + 0.3, next_start - 0.05)
            if new_end > duration:
                new_end = duration
            cleaned.append({**seg, "end": new_end})
        segments = cleaned

    # Build text clips — each segment gets its own shadow + text
    text_layers = []

    # Title overlay — pick ONE strategy based on whether there's a
    # real instrumental intro:
    # - intro >= 3s of silence before first lyric: cinematic centered
    #   "drop" title that fills the frame and fades just before the
    #   first sung line.
    # - no real intro (first lyric near t=0): compact top-third title
    #   card for the first 5s, top placement so it doesn't fight the
    #   centered subtitles.
    # Never both — they were rendering simultaneously when first_lyric
    # was past 3s, leaving "ARTIST/Title" stamped at top while the big
    # drop title also showed centered.
    first_lyric_start = segments[0]["start"] if segments else duration
    # Prefer the title the user explicitly set on the job — falls back to
    # parsing the filename only when the job didn't carry one (legacy rows
    # or batch CLI uploads). The filename heuristic handles both
    # "Artist - Title" and "Title_Artist" so a Suno-style export still
    # gets a real overlay rendered.
    if song_title:
        title_song = song_title
    else:
        raw_name = os.path.splitext(os.path.basename(mp3_path))[0]
        title_song = raw_name
        if " - " in raw_name:
            title_song = raw_name.split(" - ", 1)[1]
        elif "_" in raw_name:
            title_song = raw_name.split("_", 1)[0]
        for sfx in ["(Official Video)", "(Official Audio)", "(Lyric Video)",
                     "(Official Music Video)", "(Audio)", "(Video)", "(En Vivo)",
                     "(Live)", "(Lyrics)"]:
            title_song = title_song.replace(sfx, "").strip()

    # Defensive scrub: even when the upload pipeline pre-parsed the title,
    # legacy rows (or a manually-typed value) can still carry the raw
    # "Title_Artist" or "Artist - Title" filename basename. Re-parse so the
    # overlay never shows a literal underscore-joined filename like
    # "No Tengo Ganas_Intoxicados".
    if title_song:
        if " - " in title_song and artist and title_song.startswith(artist):
            title_song = title_song.split(" - ", 1)[1].strip()
        if artist and title_song.endswith(f"_{artist}"):
            title_song = title_song[: -(len(artist) + 1)].strip()
        if "_" in title_song and not artist:
            title_song = title_song.split("_", 1)[0].strip()

    if artist or title_song:
        # Artist name renders in ExtraBold (heavier weight) to visually
        # distinguish it from the song title, which stays in Bold.
        extrabold_font = os.path.join(_FONTS_DIR, "Montserrat-ExtraBold.ttf")
        if not os.path.exists(extrabold_font):
            extrabold_font = font  # graceful fallback

        artist_upper = artist.upper() if artist else ""
        title_display = title_song if title_song else ""

        # The title card MUST always appear — users (UMG, internal QA)
        # want the artist+song readable on every video. Two layouts:
        #
        #   LONG intro (>0.8 s before first lyric):
        #     Centered "card" with large artist+song. Fades in/out over
        #     the intro period before the first lyric. Same visual as
        #     before the 2026-05-11 rewrite.
        #
        #   SHORT intro (≤0.8 s; Whisper often hallucinates the first
        #   "lyric" near t=0 even when there's a real instrumental):
        #     Compact lower-left "lower-third" overlay. Smaller font,
        #     sits in the bottom-left corner so it doesn't overlap the
        #     centred lyric line. Visible for 6 s, with crossfade in/out.
        #
        # The old code skipped the title entirely on short intros,
        # producing the "title NEVER appears" bug the user reported.
        #
        # Implementation note: moviepy 1.0.3 accepts crossfadein/
        # crossfadeout as clip transforms, but its set_opacity broke
        # when passed a function (TypeError: 'function' * 'float').
        # We now use STATIC opacity + crossfade transforms, which work
        # uniformly across moviepy versions.
        try:
            from moviepy.video.fx.crossfadein import crossfadein
            from moviepy.video.fx.crossfadeout import crossfadeout
        except Exception:  # pragma: no cover — older moviepy paths
            crossfadein = crossfadeout = None

        try:
            scale = spec.text_scale
            START_T = 0.3            # delay before card appears

            # Decide layout based on how much intro time we have. 0.8 s
            # is the minimum window for a readable centred card; below
            # that the user can't actually read it before the lyrics
            # take over.
            has_long_intro = first_lyric_start > START_T + 0.5

            if has_long_intro:
                # ----- CENTRED FULL CARD (long intro) -----
                artist_size = max(30, int(round(62 * scale)))
                title_size = max(24, int(round(46 * scale)))
                card_width = int(round(spec.width * 0.80))
                stroke_w = max(1, int(round(1.6 * scale)))
                title_end = min(first_lyric_start - 0.2, START_T + 8.0)
                clip_dur = title_end - START_T
                # Fades scale down for short available windows so we
                # always get at least a moment of full opacity.
                fade_in = min(0.4, max(0.1, clip_dur * 0.25))
                fade_out = min(0.7, max(0.1, clip_dur * 0.35))
                position_y_center = True
                position_x_center = True
                base_opacity_artist = 0.97
                base_opacity_song = 0.85
            else:
                # ----- LOWER-LEFT BADGE (short intro fallback) -----
                # Smaller because it sits next to the active lyric line;
                # we never want it to compete for the user's attention,
                # just provide identification.
                artist_size = max(20, int(round(36 * scale)))
                title_size = max(16, int(round(28 * scale)))
                card_width = int(round(spec.width * 0.45))
                stroke_w = max(1, int(round(1.2 * scale)))
                title_end = START_T + 6.0
                clip_dur = title_end - START_T
                fade_in = 0.4
                fade_out = 0.8
                position_y_center = False     # bottom-anchored
                position_x_center = False     # left-anchored
                base_opacity_artist = 0.92
                base_opacity_song = 0.80
                print(
                    f"[TITLE] first lyric at {first_lyric_start:.2f}s — "
                    f"using lower-left badge (intro too short for centred card)"
                )

            title_card_clips = []

            if artist_upper:
                artist_clip = TextClip(
                    artist_upper, fontsize=artist_size, font=extrabold_font,
                    color="white", stroke_color="black", stroke_width=stroke_w,
                    method="caption", size=(card_width, None), align="center" if position_x_center else "West",
                )
                title_card_clips.append((artist_clip, base_opacity_artist))

            if title_display:
                song_clip = TextClip(
                    title_display, fontsize=title_size, font=font,
                    color="white", stroke_color="black", stroke_width=max(1, int(round(1.2 * scale))),
                    method="caption", size=(card_width, None), align="center" if position_x_center else "West",
                )
                title_card_clips.append((song_clip, base_opacity_song))

            if title_card_clips:
                total_h = sum(c.size[1] for c, _ in title_card_clips) + 8 * (len(title_card_clips) - 1)

                if position_y_center:
                    y_cursor = (spec.height - total_h) // 2
                else:
                    # Bottom margin = 8% of frame height — comfortable
                    # safe-area for broadcast and YouTube.
                    bottom_margin = int(spec.height * 0.08)
                    y_cursor = spec.height - bottom_margin - total_h

                if position_x_center:
                    cx = spec.width // 2
                else:
                    # Left margin = 6% of frame width.
                    left_margin = int(spec.width * 0.06)

                for clip, base_op in title_card_clips:
                    cw, ch = clip.size
                    if position_x_center:
                        x = cx - cw // 2
                    else:
                        x = left_margin
                    clip = (clip
                            .set_opacity(base_op)
                            .set_position((x, y_cursor))
                            .set_start(START_T).set_end(title_end))
                    # Apply crossfade transforms if moviepy provides them.
                    # Skipping fades on older paths still beats no title.
                    if crossfadein is not None and crossfadeout is not None:
                        clip = clip.fx(crossfadein, fade_in).fx(crossfadeout, fade_out)
                    text_layers.append(clip)
                    y_cursor += ch + 8
        except Exception as e:
            print(f"[TITLE] title card failed ({e}); continuing")

    for seg in segments:
        layers = _make_text_clip(
            seg["text"], seg["start"], seg["end"], font, spec=spec,
            text_case=text_case, font_scale=font_scale,
            lyric_transition=lyric_transition, text_motion=text_motion,
            text_contrast=text_contrast,
        )
        text_layers.extend(layers)

    video = CompositeVideoClip([bg] + text_layers, size=(spec.width, spec.height))
    video = video.set_audio(audio).set_duration(duration)

    if spec.profile == "umg":
        out_path = os.path.join(job_dir, "umg_master.mov")
        ffmpeg_params = [
            "-r", spec.fps_str,
            "-profile:v", str(spec.prores_profile),
            "-pix_fmt", spec.pix_fmt,
            "-vendor", "apl0",
            "-color_primaries", "bt709",
            "-color_trc", "bt709",
            "-colorspace", "bt709",
            "-color_range", "tv",
            "-aspect", f"{spec.dar[0]}:{spec.dar[1]}",
            "-vf", "setsar=1",
        ]
        # moviepy 1.0.3 writes audio at the source MP3 rate (typically
        # 44.1 kHz). `audio_fps=48000` triggers a moviepy bug where it
        # mixes -c:a copy with an aresample filter and ffmpeg refuses
        # the combo, so we resample in a separate ffmpeg pass after the
        # moviepy write — two steps but each one stays in its lane.
        video.write_videofile(
            out_path,
            fps=spec.fps,
            codec=spec.codec,
            audio_codec=spec.audio_codec,
            ffmpeg_params=ffmpeg_params,
            threads=4,
            logger=None,
        )
        audio.close()
        bg.close()
        video.close()

        # Post-process: stream-copy the ProRes video and re-encode audio
        # to pcm_s24le at 48 kHz. UMG requires this exact audio spec; no
        # CPU is wasted re-encoding the multi-GB ProRes stream.
        tmp_resampled = out_path + ".audio48k.mov"
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", out_path,
                "-c:v", "copy",
                "-c:a", "pcm_s24le",
                "-ar", "48000",
                "-ac", "2",
                tmp_resampled,
            ],
            capture_output=True, text=True, timeout=600,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"audio resample to 48kHz failed: {result.stderr[-500:]}"
            )
        os.replace(tmp_resampled, out_path)

        errors = _validate_umg_master(out_path, spec)
        if errors:
            raise RuntimeError(f"UMG validation failed: {'; '.join(errors)}")
        return out_path, font, bg_source

    out_path = os.path.join(job_dir, "lyric_video.mp4")
    video.write_videofile(
        out_path,
        fps=spec.fps,
        codec=spec.codec,
        audio_codec=spec.audio_codec,
        threads=4,
        logger=None,
    )
    audio.close()
    bg.close()
    video.close()
    return out_path, font, bg_source


# ---------------------------------------------------------------------------
# Step 3 — YouTube Short (30s, vertical)
# ---------------------------------------------------------------------------

def _find_chorus_start(segments: list[dict], window_sec: int = 30) -> float:
    """Find the start of the chorus — the 30s window with most repeated lyrics."""
    if not segments:
        return 0.0

    if not segments[-1].get("end"):
        return 0.0

    total_duration = segments[-1]["end"]

    # Count how many times each line appears (normalized)
    from collections import Counter
    line_counts = Counter()
    for seg in segments:
        normalized = seg["text"].strip().lower()
        if len(normalized) > 5:  # skip very short fragments
            line_counts[normalized] += 1

    # Score each segment: repeated lines get higher scores
    for seg in segments:
        normalized = seg["text"].strip().lower()
        seg["_chorus_score"] = line_counts.get(normalized, 0)

    # Slide a window and find the 30s with highest total chorus score
    best_start = 0.0
    best_score = -1
    step = 1.0
    t = 0.0
    while t + window_sec <= total_duration + step:
        score = sum(
            seg["_chorus_score"]
            for seg in segments
            if seg["start"] >= t and seg["end"] <= t + window_sec
        )
        if score > best_score:
            best_score = score
            best_start = t
        t += step

    # Clean up temp keys
    for seg in segments:
        seg.pop("_chorus_score", None)

    # Clamp to valid range
    best_start = max(0, min(best_start, total_duration - window_sec))
    print(f"[SHORT] Chorus detected at {best_start:.1f}s (score={best_score})")
    return best_start


def _make_short_text_clip(text: str, seg_start: float, seg_end: float, font: str = "Arial"):
    """Create text clips sized for vertical 1080x1920 short.

    Sizes are tuned for TikTok / Reels / Shorts viewing on mobile — the
    previous defaults (40 / 50 / 65) read too small on phones held at arm's
    length. Bumped to 75 / 95 / 115 which fills more of the vertical
    real-estate and matches what creators on those platforms actually use.
    """
    display_text = text.upper()

    text_len = len(display_text)
    if text_len > 60:
        fontsize = 75
        text_width = 1000
    elif text_len > 35:
        fontsize = 95
        text_width = 980
    else:
        fontsize = 115
        text_width = 950

    shadow = TextClip(
        display_text,
        fontsize=fontsize,
        font=font,
        color="black",
        method="caption",
        size=(text_width, None),
        align="center",
    ).set_opacity(0.6)

    sh = shadow.size[1]
    shadow_y = (1920 - sh) // 2 + 4
    shadow_x = (1080 - text_width) // 2 + 4
    shadow = shadow.set_position((shadow_x, shadow_y)).set_start(seg_start).set_end(seg_end)

    txt = TextClip(
        display_text,
        fontsize=fontsize,
        font=font,
        color="white",
        stroke_color="black",
        stroke_width=3,
        method="caption",
        size=(text_width, None),
        align="center",
    ).set_position(("center", "center")).set_start(seg_start).set_end(seg_end)

    return [shadow, txt]


def generate_short(
    mp3_path: str,
    segments: list[dict],
    job_dir: str,
    bg_source: str | None = None,
    style: str = "oscuro",
    font: str = "Arial",
    fps: float = 24,
) -> str:
    """Generate a 1080x1920 vertical short from the chorus section.

    `fps` is propagated to the final write so the lazy ProRes short can
    do a pure recode when UMG asks for a non-24 frame rate. Stays at 24
    by default for the YouTube-only path.
    """
    audio = AudioFileClip(mp3_path)
    start_time = _find_chorus_start(segments)
    end_time = min(start_time + 30, audio.duration)
    short_dur = end_time - start_time
    short_audio = audio.subclip(start_time, end_time)

    # Build vertical background from RAW source (no burned-in lyrics)
    if bg_source and bg_source.lower().endswith((".jpg", ".jpeg", ".png")):
        bg_full = _ken_burns_clip(bg_source, short_dur)
        bg = _cover_resize(bg_full, 1080, 1920)
    elif bg_source and os.path.exists(bg_source):
        try:
            raw = VideoFileClip(bg_source)
            raw.get_frame(0)
            raw = _cover_resize(raw, 1080, 1920)
            if raw.duration >= short_dur:
                bg = raw.subclip(0, short_dur)
            else:
                loops = math.ceil(short_dur / raw.duration) + 1
                clips = []
                for i in range(loops):
                    c = _cover_resize(VideoFileClip(bg_source), 1080, 1920)
                    clips.append(c)
                bg = concatenate_videoclips(clips).subclip(0, short_dur)
        except Exception:
            bg = _cover_resize(_make_gradient_clip(short_dur, style), 1080, 1920)
    else:
        bg = _cover_resize(_make_gradient_clip(short_dur, style), 1080, 1920)

    # Build text clips for segments in this 30s window
    text_layers = []
    for seg in segments:
        if seg["end"] <= start_time or seg["start"] >= end_time:
            continue
        s = max(0, seg["start"] - start_time)
        e = min(short_dur, seg["end"] - start_time)
        if e - s < 0.1:
            continue
        layers = _make_short_text_clip(seg["text"], s, e, font)
        text_layers.extend(layers)

    final = CompositeVideoClip([bg] + text_layers, size=(1080, 1920))
    final = final.set_audio(short_audio).set_duration(short_dur)

    out_path = os.path.join(job_dir, "short.mp4")
    final.write_videofile(
        out_path,
        fps=fps,
        codec="libx264",
        audio_codec="aac",
        threads=4,
        logger=None,
    )
    audio.close()
    final.close()
    return out_path


# ---------------------------------------------------------------------------
# Step 4 — Thumbnail
# ---------------------------------------------------------------------------

def _draw_text_with_outline(draw, xy, text, font, fill="white", outline="black", width=3):
    """Draw text with a thick outline for readability."""
    x, y = xy
    for ox in range(-width, width + 1):
        for oy in range(-width, width + 1):
            if ox != 0 or oy != 0:
                draw.text((x + ox, y + oy), text, font=font, fill=outline)
    draw.text((x, y), text, font=font, fill=fill)


def generate_thumbnail(
    artist: str,
    mp3_path: str,
    job_dir: str,
    bg_source: str | None = None,
    song_title: str = "",
) -> str:
    """Generate a thumbnail from the RAW background with artist and song name."""
    from PIL import ImageFilter, ImageEnhance

    # Grab a frame from the raw background (no burned-in lyrics)
    if bg_source and bg_source.lower().endswith((".jpg", ".jpeg", ".png")):
        img = Image.open(bg_source)
    elif bg_source and os.path.exists(bg_source):
        try:
            clip = VideoFileClip(bg_source)
            t = min(clip.duration * 0.4, clip.duration - 0.1)
            frame = clip.get_frame(t)
            clip.close()
            img = Image.fromarray(frame)
        except Exception:
            img = Image.new("RGB", (1280, 720), (30, 15, 60))
    else:
        img = Image.new("RGB", (1280, 720), (30, 15, 60))

    img = img.resize((1280, 720), Image.LANCZOS)

    # Slight darken so text is readable, but background is clearly visible
    enhancer = ImageEnhance.Brightness(img)
    img = enhancer.enhance(0.6)

    draw = ImageDraw.Draw(img)
    # Prefer the structured title from the job; fall back to the filename
    # only when it's missing. The filename heuristic handles both
    # "Artist - Title" and Suno-style "Title_Artist" so the thumbnail
    # never shows a literal underscore-joined basename.
    if song_title:
        song_name = song_title
    else:
        raw_name = os.path.splitext(os.path.basename(mp3_path))[0]
        song_name = raw_name
        if " - " in raw_name:
            song_name = raw_name.split(" - ", 1)[1]
        elif "_" in raw_name:
            song_name = raw_name.split("_", 1)[0]
    for suffix in ["(Official Video)", "(Official Audio)", "(Lyric Video)",
                   "(Official Music Video)", "(Audio)", "(Video)", "(En Vivo)",
                   "(Live)", "(Lyrics)"]:
        song_name = song_name.replace(suffix, "").strip()
    # Defensive scrub for legacy / manually-typed values that still carry
    # the artist concatenated to the title.
    if song_name:
        if " - " in song_name and artist and song_name.startswith(artist):
            song_name = song_name.split(" - ", 1)[1].strip()
        if artist and song_name.endswith(f"_{artist}"):
            song_name = song_name[: -(len(artist) + 1)].strip()

    # Use Montserrat ExtraBold for thumbnails (Google Font, OFL licensed)
    thumb_font = os.path.join(_FONTS_DIR, "Montserrat-ExtraBold.ttf")
    if not os.path.exists(thumb_font) and _FONT_POOL:
        thumb_font = _FONT_POOL[0]

    # Auto-shrink font until the text fits within max_width with a 60px
    # margin on each side. Without this, long artist names ("El Plan de la
    # Mariposa") or songs with explanatory subtitles overflow 1280px and
    # get cropped by the thumbnail frame.
    def _fit_font(text: str, start_size: int, max_width: int, min_size: int = 28):
        size = start_size
        while size > min_size:
            try:
                f = ImageFont.truetype(thumb_font, size)
            except (OSError, IOError):
                return ImageFont.load_default()
            bbox = draw.textbbox((0, 0), text, font=f)
            tw = bbox[2] - bbox[0]
            if tw <= max_width:
                return f
            size -= 4
        try:
            return ImageFont.truetype(thumb_font, min_size)
        except (OSError, IOError):
            return ImageFont.load_default()

    max_w = 1280 - 120  # 60px margin each side
    font_artist = _fit_font(artist.upper(), 100, max_w)
    font_song = _fit_font(song_name, 55, max_w)

    # Artist name centered
    bbox = draw.textbbox((0, 0), artist.upper(), font=font_artist)
    tw = bbox[2] - bbox[0]
    x = (1280 - tw) // 2
    _draw_text_with_outline(draw, (x, 240), artist.upper(), font_artist, fill="white", width=5)

    # Song name centered below
    bbox = draw.textbbox((0, 0), song_name, font=font_song)
    tw = bbox[2] - bbox[0]
    x = (1280 - tw) // 2
    _draw_text_with_outline(draw, (x, 380), song_name, font_song, fill=(230, 230, 240), width=3)

    out_path = os.path.join(job_dir, "thumbnail.jpg")
    img.save(out_path, "JPEG", quality=92)
    return out_path


# ---------------------------------------------------------------------------
# Edit pipeline — partial re-render at the review stage
# ---------------------------------------------------------------------------

_MAX_EDITS = 3


def run_edit_pipeline(
    job_id: str,
    edit_type: str,
    edit_params: dict,
) -> None:
    """Partial re-render triggered from POST /edit/{job_id}.

    edit_type:
        "typography" — keep existing background + segments; only re-render
            with new font/size/case/transition settings.  Cost: ~$0.
        "background" — re-generate Veo background; keep segments and
            (optionally) render params.  Cost: ~$0.90.
        "lyrics"     — keep cached background; replace segments with the
            caller-supplied list (edit_params["segments"]). Re-renders
            video/short/thumbnail.  Cost: ~$0. After success, the new
            segments overwrite segments_json so subsequent edits see
            the corrected version.

    After completion the job returns to "pending_review" so the reviewer
    can approve, reject, or request another edit (up to _MAX_EDITS total).
    """
    from database import SessionLocal, Job as JobModel

    db = SessionLocal()
    try:
        job_row = db.query(JobModel).filter(JobModel.job_id == job_id).first()
        if not job_row:
            raise RuntimeError(f"Job {job_id} not found")
        # Source of segments depends on edit_type. Lyrics edit uses the
        # caller-supplied list (they're the new "ground truth"); the
        # other two reuse what's already persisted.
        if edit_type == "lyrics":
            segments = edit_params.get("segments")
            if not segments or not isinstance(segments, list):
                raise RuntimeError(
                    f"Job {job_id}: edit_type='lyrics' requires non-empty segments in edit_params"
                )
        else:
            segments = job_row.segments_json
            if not segments:
                raise RuntimeError(f"Job {job_id} has no persisted segments — cannot edit")
        base_params = dict(job_row.render_params or {})
        artist = job_row.artist
        song_title = job_row.song_title or ""
        style = base_params.get("style") or job_row.style or "oscuro"
        delivery_profile = job_row.delivery_profile or "youtube"
        wants_youtube = delivery_profile in ("youtube", "both")
        wants_umg = delivery_profile in ("umg", "both")
        umg_spec = job_row.umg_spec
        tenant_id = job_row.tenant_id
        bg_r2_key_cached = job_row.bg_r2_key_cached
        input_r2_key = job_row.input_r2_key
    finally:
        db.close()

    # Merge base render params with the requested overrides.
    merged = {**base_params, **edit_params}
    font_id = merged.get("font") or ""
    text_case = merged.get("text_case") or "upper"
    font_scale = float(merged.get("font_scale") or 1.0)
    lyric_transition = merged.get("lyric_transition") or "cut"
    text_motion = merged.get("text_motion") or "none"
    genre = merged.get("genre") or ""
    concept = merged.get("concept") or ""
    movement_style = merged.get("movement_style") or ""
    # Per-edit operator hint for background regen (set by /edit when the
    # user typed in the "Aclarar tipo de fondo" textarea). None if absent;
    # propagates only into the `background` branch below.
    background_hint = edit_params.get("background_hint") or None

    job_dir = os.path.join(OUTPUTS_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    try:
        # ----------------------------------------------------------------
        # Fetch the source audio
        # ----------------------------------------------------------------
        mp3_path = os.path.join(job_dir, "source_audio.mp3")
        if not os.path.exists(mp3_path):
            if input_r2_key and storage.is_enabled():
                ok = storage.download_object(input_r2_key, mp3_path)
                if not ok:
                    raise RuntimeError("Could not download source audio from R2")
            else:
                raise RuntimeError("Source audio not available locally and no R2 key")

        # ----------------------------------------------------------------
        # Resolve background
        # ----------------------------------------------------------------
        if edit_type in ("typography", "lyrics"):
            # Both reuse the cached background — only the foreground layer
            # (text overlays) changes. Lyrics edit ALSO swaps the segments,
            # but that already happened at function entry above.
            update_job(job_id, status="editing", current_step="video", progress=35)
            bg_image_path = os.path.join(job_dir, "bg_cached_edit.mp4")
            if not os.path.exists(bg_image_path):
                if bg_r2_key_cached and storage.is_enabled():
                    ok = storage.download_object(bg_r2_key_cached, bg_image_path)
                    if not ok:
                        raise RuntimeError("Could not download cached background from R2")
                else:
                    raise RuntimeError(
                        f"No cached background available for {edit_type} edit. "
                        "Use edit_type='background' to regenerate it."
                    )

        elif edit_type == "background":
            update_job(job_id, status="editing", current_step="background", progress=22)
            lyrics_text = " ".join(seg["text"] for seg in segments)
            bg_image_path = _ensure_background(
                style, job_dir,
                lyrics_text=lyrics_text, artist=artist, job_id=job_id,
                song_title=song_title, genre=genre, concept=concept,
                movement_style=movement_style,
                background_hint=background_hint,
            )
            update_job(job_id, progress=35)
            # Re-cache the new background so future typography edits work.
            if bg_image_path and os.path.exists(bg_image_path) and storage.is_enabled():
                try:
                    _bg_ext = os.path.splitext(bg_image_path)[1] or ".mp4"
                    new_bg_key = storage.upload_file(
                        bg_image_path,
                        f"backgrounds/{job_id}/bg_cached{_bg_ext}",
                    )
                    if new_bg_key:
                        update_job(job_id, bg_r2_key_cached=new_bg_key)
                except Exception as _e:
                    print(f"[EDIT] Warning: re-cache of new background failed: {_e}")
        else:
            raise ValueError(f"Unknown edit_type {edit_type!r}")

        # ----------------------------------------------------------------
        # Resolve font
        # ----------------------------------------------------------------
        chosen_font = _resolve_font(font_id)
        if chosen_font:
            print(f"[EDIT] Operator font: {os.path.basename(chosen_font)}")

        # ----------------------------------------------------------------
        # Re-render video
        # ----------------------------------------------------------------
        update_job(job_id, current_step="video", progress=40)
        intermediate_spec = (
            RenderSpec.umg_intermediate_master(umg_spec) if wants_umg
            else None
        )
        _, chosen_font, bg_source = generate_lyric_video(
            mp3_path, segments, style, job_dir, artist, bg_image_path,
            font=chosen_font, spec=intermediate_spec,
            song_title=song_title,
            text_case=text_case,
            font_scale=font_scale,
            lyric_transition=lyric_transition,
            text_motion=text_motion,
        )
        files = {"video_url": f"/download/{job_id}/video"}
        update_job(job_id, progress=55)

        if wants_umg:
            files["umg_master_url"] = f"/download/{job_id}/umg_master"
            files["umg_short_url"] = f"/download/{job_id}/umg_short"

        # ----------------------------------------------------------------
        # Re-render short + thumbnail
        # ----------------------------------------------------------------
        if wants_youtube or wants_umg:
            update_job(job_id, current_step="short", progress=75)
            short_fps = float(umg_spec["fps"]) if wants_umg and umg_spec else 24
            generate_short(
                mp3_path, segments, job_dir, bg_source=bg_source,
                style=style, font=chosen_font, fps=short_fps,
            )
            files["short_url"] = f"/download/{job_id}/short"
            update_job(job_id, progress=85)

            update_job(job_id, current_step="thumbnail", progress=90)
            generate_thumbnail(artist, mp3_path, job_dir, bg_source=bg_source, song_title=song_title)
            files["thumbnail_url"] = f"/download/{job_id}/thumbnail"

        # ----------------------------------------------------------------
        # Verify + upload to R2 (replacing previous deliverables)
        # ----------------------------------------------------------------
        try:
            audio_dur = _audio_duration(mp3_path)
        except Exception:
            audio_dur = _ffprobe_duration(mp3_path)
        _verify_deliverables(job_dir, files, audio_dur)

        s3_keys = _upload_deliverables_to_r2(job_id, job_dir, files)
        if s3_keys:
            update_job(job_id, s3_keys=s3_keys)

        _cleanup_local_intermediates(job_dir)

        # Persist the merged render params so the next edit sees them.
        update_job(job_id, render_params=merged)

        # For lyrics edits, also persist the new segments so subsequent
        # actions (another edit, a retry) see the corrected version.
        # Without this, the corrections would only live in the rendered
        # video bytes; any later run_pipeline call would read the OLD
        # segments_json and re-render with the bad words.
        if edit_type == "lyrics":
            update_job(job_id, segments_json=segments)

        # Back to pending_review — the reviewer decides what to do next.
        update_job(job_id, status="pending_review", progress=100, files=files)
        print(f"[EDIT] job={job_id} edit_type={edit_type} → pending_review")

    except Exception as exc:
        print(f"[EDIT] job={job_id} FAILED: {exc}")
        update_job(job_id, status="error", error=f"Edit failed: {exc}")
        raise
