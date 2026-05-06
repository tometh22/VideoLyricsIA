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


def run_pipeline(job_id: str, mp3_path: str, artist: str, style: str,
                 language: str = None, segments_override: list[dict] = None,
                 delivery_profile: str = "youtube", umg_spec: dict | None = None,
                 background_path: str = None,
                 input_r2_key: str | None = None,
                 bg_r2_key: str | None = None,
                 genre: str = "",
                 font: str = "",
                 concept: str = "",
                 movement_style: str = "",
                 animate_image: bool = False):
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

    wants_youtube = delivery_profile in ("youtube", "both")
    wants_umg = delivery_profile in ("umg", "both")

    try:
        # Step 1 — Whisper transcription (or use edited segments)
        update_job(job_id, current_step="whisper", progress=5)
        if segments_override:
            segments = segments_override
            print(f"[WHISPER] Using {len(segments)} user-edited segments")
        else:
            segments = transcribe(mp3_path, language=language)
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
            # Extract a clean song title from the MP3 filename so Gemini gets
            # song-level context even when the lyrics transcription degrades.
            # The cache key downstream uses (artist|title) as a namespace so
            # different songs don't share a Veo background.
            _basename = os.path.splitext(os.path.basename(mp3_path))[0]
            _song_title = _basename.split(" - ", 1)[1] if " - " in _basename else _basename
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
            )
            # Image-to-video fallback: if Veo failed to produce an MP4 (None
            # or non-existent path) AND the operator wanted to animate their
            # image, fall back to using the still image with Ken Burns.
            if _animate_user_image and (not bg_image_path or not os.path.exists(bg_image_path)):
                print(f"[BG] image-to-video failed, falling back to Ken Burns "
                      f"on {background_path}")
                bg_image_path = background_path
        update_job(job_id, progress=40)

        files = {}
        # When the operator picked an explicit font id, resolve it to a
        # path now and seed `chosen_font`. generate_lyric_video reuses a
        # truthy `font` argument as-is and only random-picks when None.
        chosen_font = _resolve_font(font)
        if chosen_font:
            print(f"[FONT] Operator-selected: {os.path.basename(chosen_font)}")
        bg_source = bg_image_path

        # Step 2 — YouTube lyric video (H.264 / MP4 / 1080p / 24fps)
        if wants_youtube:
            update_job(job_id, current_step="video", progress=40)
            _, chosen_font, bg_source = generate_lyric_video(
                mp3_path, segments, style, job_dir, artist, bg_image_path,
                font=chosen_font,
            )
            files["video_url"] = f"/download/{job_id}/video"
            update_job(job_id, progress=55)

        # Step 2b — UMG master (ProRes / .mov / target frame size & fps)
        if wants_umg:
            if not umg_spec:
                raise RuntimeError("UMG delivery requested without umg_spec")
            update_job(job_id, current_step="umg_master", progress=58)
            spec = RenderSpec.umg(**umg_spec)
            # Reuse font/bg from YouTube render if available; otherwise pick fresh.
            _, chosen_font, bg_source = generate_lyric_video(
                mp3_path, segments, style, job_dir, artist, bg_image_path,
                spec=spec, font=chosen_font,
            )
            files["umg_master_url"] = f"/download/{job_id}/umg_master"
            update_job(job_id, progress=70)

        # Step 3 — YouTube Short (only when YouTube delivery is requested)
        if wants_youtube:
            update_job(job_id, current_step="short", progress=75)
            generate_short(
                mp3_path, segments, job_dir, bg_source=bg_source,
                style=style, font=chosen_font,
            )
            files["short_url"] = f"/download/{job_id}/short"
            update_job(job_id, progress=85)

            # Step 4 — Thumbnail (uses raw background, not lyric video)
            update_job(job_id, current_step="thumbnail", progress=90)
            generate_thumbnail(
                artist, mp3_path, job_dir, bg_source=bg_source,
            )
            files["thumbnail_url"] = f"/download/{job_id}/thumbnail"

        # Step 5 — Content validation (UMG Guideline 15) — only if a YouTube
        # video was rendered (UMG-only jobs skip validation; masters go to
        # legal review independently).
        #
        # We validate the BACKGROUND ASSET (bg_source) before lyrics overlay,
        # not the composited final. The validator looks for prohibited content
        # like people, logos, foreign text — but the final video has OUR
        # intentional lyrics burned in, which would be a guaranteed false
        # positive. The background is what we actually need to police.
        if wants_youtube and bg_source:
            update_job(job_id, current_step="validation", progress=94)
            ext = os.path.splitext(bg_source)[1].lower()
            if ext in (".mp4", ".mov", ".webm"):
                from content_validator import validate_video as _validate_bg
            else:
                from content_validator import validate_image as _validate_bg
            validation = _validate_bg(bg_source, job_id=job_id)
            update_job(job_id, validation_result=validation)

            if not validation["passed"]:
                update_job(
                    job_id,
                    status="validation_failed",
                    error=f"Content policy violation detected: {validation['issues']}",
                )
                print(f"[VALIDATION] FAILED for job {job_id}: {validation['issues']}")
                return

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
    except Exception as exc:
        traceback.print_exc()
        update_job(job_id, status="error", error=str(exc))


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


_WHISPER_MODELS: dict = {}
_WHISPER_LOCK = None


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
        )
        if os.path.exists(out) and os.path.getsize(out) > 0:
            new_sz = os.path.getsize(out)
            print(f"[WHISPER-API] compressed {sz/1e6:.1f} MB → "
                  f"{new_sz/1e6:.1f} MB for API limit")
            return out
    except (_sp.CalledProcessError, _sp.TimeoutExpired,
            FileNotFoundError, OSError) as e:
        print(f"[WHISPER-API] compression failed ({e}); sending original")
    return input_path


def _transcribe_via_openai_api(mp3_path: str, language: str | None = None,
                                lyrics_hint: str | None = None) -> list[dict]:
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
    try:
        with open(api_path, "rb") as f:
            kwargs["file"] = f
            response = client.audio.transcriptions.create(**kwargs)
    finally:
        if cleanup_compressed:
            try:
                os.unlink(api_path)
            except OSError:
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

    print(f"[WHISPER-API] {len(segments)} segments")
    return segments


def transcribe(mp3_path: str, language: str = None,
               lyrics_hint: str | None = None) -> list[dict]:
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


def _fetch_lrclib(artist: str, song: str) -> dict | None:
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

    Best-effort — never raises.
    """
    if not artist or not song:
        return None
    import requests as _req
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
        return None
    try:
        if r.status_code != 200:
            print(f"[LYRICS] lrclib {r.status_code} for {artist!r} - {song!r}")
            return None
        data = r.json()
        plain = (data.get("plainLyrics") or "").strip() or None
        synced = (data.get("syncedLyrics") or "").strip() or None
        if not plain and not synced:
            return None
        # Some lrclib records expose only `syncedLyrics` (different bots
        # populate the two columns independently). The downstream auto-
        # recover code in /transcribe gates on `if plain:` so when plain
        # is missing the recovery branch is unreachable. Derive plain
        # from synced by stripping the `[mm:ss.xx]` timestamps so the
        # recovery path always has a usable reference. This keeps El
        # Plan de la Mariposa - El Riesgo (which has only syncedLyrics
        # in some lrclib records) from falling all the way through to
        # the no-recovery Gemini fallback.
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
    except Exception as e:
        print(f"[LYRICS] lrclib fetch failed: {e}")
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

    return False, ""


def _has_fuzzy_intra_loop(text: str) -> bool:
    """Detect 3+ near-duplicate consecutive word-windows in a segment.
    Two windows count as the same loop when their token-set Jaccard is
    ≥ 0.75 — catches synonym swaps ("reflexionar" ↔ "pensar") that the
    exact-equality intra-loop truncator misses.

    Window sizes 4..14 (longer first), same shape as the existing
    `_truncate_intra_loop`, but only used as a SIGNAL here, not a fix.
    """
    words = text.split()
    total = len(words)
    if total < 12:
        return False
    for window in range(14, 3, -1):
        if total < window * 3:
            continue
        for start in range(total - window * 3 + 1):
            phrase_set = set(w.lower() for w in words[start:start + window])
            if not phrase_set:
                continue
            count = 1
            pos = start + window
            while pos + window <= total:
                next_set = set(w.lower() for w in words[pos:pos + window])
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

    plain_token_sets = [
        set(w.lower() for w in line.split()) for line in plain_lines
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
        seg_set = set(w.lower() for w in text.split())
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


def _whisper_quick_text(mp3_path: str) -> str:
    """Minimal whisper-1 transcription of a short clip — used by alignment
    verification. Returns plain text with no post-processing (no spam
    filter, no dedup). Best-effort: returns "" on any failure.
    """
    if not os.path.exists(mp3_path):
        return ""
    try:
        from openai import OpenAI
        with open(mp3_path, "rb") as f:
            r = OpenAI().audio.transcriptions.create(
                model="whisper-1", file=f, response_format="text",
            )
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
                return row.lyrics
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
                                    movement_style: str = "") -> dict:
    """Use Gemini to analyze lyrics and choose visual style + prompt.

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

    if normalized_concept:
        # Operator picked an explicit visual concept — that hard-overrides
        # the genre's scene vocabulary. The concept is the controlling
        # input here; genre still goes into the user content as a stylistic
        # color hint but does NOT determine the scene type.
        concept_guide = _CONCEPT_SCENE_GUIDE[normalized_concept]
        genre_hint = (f"\n\nFor stylistic colour-grading flavour only "
                      f"(NOT for scene choice), the song genre is: "
                      f"{normalized_genre.upper()}.") if normalized_genre else ""
        system_prompt = f"""Respond ONLY with a JSON object, no other text. Example:
{{"style":"video","prompt":"Slow tracking shot through neon-lit rain-slicked streets, deep blue and red reflections, smoke rising past streetlamps, gritty cinematic 4k"}}

The operator has explicitly requested a {normalized_concept.upper()} background.

You MUST pick a scene that fits this concept's visual vocabulary:
{concept_guide}{genre_hint}

Hard rules:
- "style" must always be "video"
- "prompt" is 20-40 words: scene + camera movement + colors + lighting + atmosphere
- Pick a DIFFERENT specific scene each time (don't repeat across songs)
- The concept choice is binding — do NOT drift to a different visual category
- Never include people, faces, hands, or readable text in the scene{movement_extra_line}"""
    elif normalized_genre:
        # User-supplied genre: lock Gemini to that genre's visual vocabulary
        # and forbid the lazy "ocean sunset" default. This is the high-
        # certainty path — UMG operators picking the genre at upload time
        # gets us deterministic visual matching for their catalogue.
        scene_guide = _GENRE_SCENE_GUIDE[normalized_genre]
        system_prompt = f"""Respond ONLY with a JSON object, no other text. Example:
{{"style":"video","prompt":"Slow tracking shot through neon-lit rain-slicked streets, deep blue and red reflections, smoke rising past streetlamps, gritty cinematic 4k"}}

The song genre is: {normalized_genre.upper()}

You MUST pick a scene from this genre's visual vocabulary:
{scene_guide}

Hard rules:
- "style" must always be "video"
- "prompt" is 20-40 words: scene + camera movement + colors + lighting + atmosphere
- Pick a DIFFERENT specific scene each time (don't repeat across songs)
- Do NOT default to "calm ocean at sunset" unless this song is BALLAD
- Never include people, faces, hands, or readable text in the scene{movement_extra_line}"""
    else:
        # No genre hint: ask Gemini to classify first, then pick.
        # "Auto" mode for users who don't want to choose.
        system_prompt = """Respond ONLY with a JSON object, no other text. Example:
{"style":"video","prompt":"Slow tracking shot through neon-lit rain-slicked streets, deep blue and red reflections, smoke rising past streetlamps, gritty cinematic 4k"}

Step 1: Classify the song's genre using the artist, title, and lyrics. Pick ONE of:
  rock, pop, ballad, latin, reggaeton, hiphop, electronic, indie, folk, metal

Step 2: Pick a scene from the matching genre's visual vocabulary:
- rock     → urban industrial streets, neon alleyways, gritty rain, electric storms, abandoned warehouses
- pop      → vibrant neon, disco reflections, geometric light patterns, glossy gradient skies
- ballad   → soft sunset, calm ocean, drifting clouds, warm golden light, candlelight
- latin    → tropical beaches, palm trees, vibrant flowers, festive lanterns, sunlit caribbean water
- reggaeton → night cityscape with red/pink neon, luxury cars, club lasers
- hiphop   → city skyline at night with gold, marble luxury textures, smoke-filled spotlights
- electronic → abstract geometry, particle storms, fractal liquid metal, laser grids
- indie    → misty forests, vintage interiors, autumn roads, lone lighthouses, dreamy lakes
- folk     → mountain vistas, dusty roads, wheat fields, riverside campfires
- metal    → volcanic lava streams, dark cathedrals, stormy lightning, cracked obsidian

Step 3: Output JSON with the chosen scene as a 20-40 word prompt.

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
    user_content = (
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
                       movement_style: str = "") -> dict:
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

    operation_name: str | None = None
    for attempt in range(5):
        try:
            print(f"[BG] Veo 3: generating video (attempt {attempt + 1}/5)...")
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
                wait = 60 * (attempt + 1)
                print(f"[BG] Rate limited (HTTP {r.status_code}), waiting {wait}s before retry...")
                _time.sleep(wait)
                continue
            if not r.ok:
                detail = r.text[:500]
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
            print(f"[BG] Veo 3 attempt {attempt + 1} request error: {e}")
            wait = 60 * (attempt + 1)
            _time.sleep(wait)
    else:
        if recorder:
            recorder.finish(response_summary="error: rate_limit_exceeded_after_5_retries")
        raise RuntimeError("Veo 3 rate limit exceeded after 5 retries")

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


def _ensure_background(style_hint: str, job_dir: str, lyrics_text: str = None,
                       artist: str = "", job_id: str = None,
                       song_title: str = "", genre: str = "",
                       concept: str = "",
                       movement_style: str = "",
                       image_to_video_path: str | None = None) -> str:
    """Generate background using AI. Gemini picks the best style for the song.

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
        concept=concept, movement_style=movement_style,
    )
    prompt = result["prompt"]

    bg_path = os.path.join(job_dir, "bg_generated.mp4")
    import time as _time_bg
    for attempt in range(3):
        try:
            _generate_veo_video(
                prompt, bg_path, job_id=job_id,
                cache_namespace=f"{artist}|{song_title}",
                image_path=image_to_video_path,
                movement_style=movement_style,
            )
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


def _find_background_video() -> str | None:
    """Pick a random background video without repeating until all are used."""
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

    # Filter out already used; if all used, reset the cycle
    available = [v for v in all_videos if v not in used]
    if not available:
        print(f"[BG] All {len(all_videos)} backgrounds used, resetting cycle")
        used = []
        available = all_videos

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


def _get_background_clip_from_path(bg_path: str, style: str, duration: float,
                                   job_dir: str = None, spec: RenderSpec | None = None):
    """Load a background video, loop it seamlessly via ffmpeg, return clip."""
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
        return _cover_resize(
            VideoFileClip(bg_path), spec.width, spec.height
        ).subclip(0, duration)

    # Pre-render the looped video with ffmpeg (fast, fluid, native fps)
    if job_dir:
        # Use per-spec filename so YouTube and UMG paths don't clobber each other.
        looped_name = f"bg_looped_{spec.width}x{spec.height}.mp4"
        looped_path = _prerender_looped_bg(
            bg_path, duration, job_dir,
            target_w=spec.width, target_h=spec.height,
            out_name=looped_name,
        )
        return VideoFileClip(looped_path)

    # Fallback: simple concatenation
    loops = math.ceil(duration / clip_dur) + 1
    clips = [_cover_resize(VideoFileClip(bg_path), spec.width, spec.height)
             for _ in range(loops)]
    return concatenate_videoclips(clips).subclip(0, duration)


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


def _make_text_clip(text: str, seg_start: float, seg_end: float, font: str = "Arial",
                    spec: RenderSpec | None = None):
    """Create a clean text clip matching pro lyric video style (bold white, subtle shadow)."""
    import unicodedata
    if spec is None:
        spec = RenderSpec.youtube_default()
    # Sanitize text: normalize unicode and remove problematic characters
    display_text = unicodedata.normalize("NFC", text.upper())
    # Remove characters that break ImageMagick's @file parsing
    display_text = display_text.replace("@", "").replace("`", "'").replace("\x00", "")

    scale = spec.text_scale

    text_len = len(display_text)
    if text_len > 80:
        fontsize = int(round(55 * scale))
        text_width = int(round(1700 * scale))
    elif text_len > 50:
        fontsize = int(round(70 * scale))
        text_width = int(round(1650 * scale))
    else:
        fontsize = int(round(85 * scale))
        text_width = int(round(1500 * scale))

    # Scale shadow offset proportionally (3 px baseline at 1080p).
    shadow_offset = max(1, int(round(3 * scale)))

    # Fallback font if the selected one fails with ImageMagick
    fallback_font = os.path.join(_FONTS_DIR, "Montserrat-Bold.ttf")

    # Stroke scales too so it stays visually similar at higher resolutions.
    stroke_width = max(1.0, 1.5 * scale)

    def _try_text_clip(text, fsize, fnt, color, **kwargs):
        try:
            return TextClip(text, fontsize=fsize, font=fnt, color=color,
                            method="caption", size=(text_width, None), align="center", **kwargs)
        except Exception:
            return TextClip(text, fontsize=fsize, font=fallback_font, color=color,
                            method="caption", size=(text_width, None), align="center", **kwargs)

    # Soft shadow for depth
    shadow = _try_text_clip(display_text, fontsize, font, "black").set_opacity(0.4)

    sh = shadow.size[1]
    shadow_y = (spec.height - sh) // 2 + shadow_offset
    shadow_x = (spec.width - text_width) // 2 + shadow_offset
    shadow = shadow.set_position((shadow_x, shadow_y)).set_start(seg_start).set_end(seg_end)

    # Main text — clean white, thin stroke
    txt = _try_text_clip(display_text, fontsize, font, "white",
                         stroke_color="black", stroke_width=stroke_width
    ).set_position("center").set_start(seg_start).set_end(seg_end)

    return [shadow, txt]


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

    # Build text clips — each segment gets its own shadow + text
    text_layers = []

    # Title overlay strategy:
    # - If there's a meaningful instrumental intro (>= 3 s), show the title
    #   BIG and centered during the intro (the cinematic "drop" feel).
    # - ALWAYS also show a small top-of-screen stamp for the first 6 s
    #   regardless of intro length, so songs that start with vocals
    #   immediately still surface artist + title to the viewer. UMG flagged
    #   this — branding the cut with the artist is mandatory for them.
    first_lyric_start = segments[0]["start"] if segments else duration
    raw_name = os.path.splitext(os.path.basename(mp3_path))[0]
    title_song = raw_name
    if " - " in raw_name:
        title_song = raw_name.split(" - ", 1)[1]
    for sfx in ["(Official Video)", "(Official Audio)", "(Lyric Video)",
                 "(Official Music Video)", "(Audio)", "(Video)", "(En Vivo)",
                 "(Live)", "(Lyrics)"]:
        title_song = title_song.replace(sfx, "").strip()

    if first_lyric_start > 3 and artist:
        title_end = first_lyric_start - 0.5
        title_layers = _make_text_clip(
            f"{artist}\n{title_song}", 0.5, title_end, font, spec=spec
        )
        text_layers.extend(title_layers)

    # Top-of-screen stamp — small, semi-transparent, never overlaps the
    # main lyric line in the center.
    if artist:
        try:
            stamp_text = f"{artist.upper()}  •  {title_song}"
            stamp_size = max(24, int(round(36 * spec.text_scale)))
            stamp = TextClip(
                stamp_text,
                fontsize=stamp_size,
                font=font,
                color="white",
                stroke_color="black",
                stroke_width=max(1, int(round(1.2 * spec.text_scale))),
                method="label",
            ).set_opacity(0.85)
            sw = stamp.size[0]
            stamp_x = (spec.width - sw) // 2
            stamp_y = max(20, int(round(40 * spec.text_scale)))
            stamp = (stamp.set_position((stamp_x, stamp_y))
                          .set_start(0.3)
                          .set_end(min(6.0, duration - 0.1)))
            text_layers.append(stamp)
        except Exception as e:
            print(f"[TITLE] top-stamp render failed ({e}); continuing without it")

    for seg in segments:
        layers = _make_text_clip(seg["text"], seg["start"], seg["end"], font, spec=spec)
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
) -> str:
    """Generate a 1080x1920 vertical short from the chorus section."""
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
        fps=24,
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
    # Extract song name from filename, removing artist prefix if present
    raw_name = os.path.splitext(os.path.basename(mp3_path))[0]
    # Handle "Artist - Song" and "Artist - Song (Extra Info)" formats
    song_name = raw_name
    if " - " in raw_name:
        song_name = raw_name.split(" - ", 1)[1]
    # Remove common suffixes
    for suffix in ["(Official Video)", "(Official Audio)", "(Lyric Video)",
                   "(Official Music Video)", "(Audio)", "(Video)", "(En Vivo)",
                   "(Live)", "(Lyrics)"]:
        song_name = song_name.replace(suffix, "").strip()

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
