"""Full processing pipeline: Whisper → Video → Short → Thumbnail."""

import hashlib
import json
import os
import math
import random
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
                 bg_r2_key: str | None = None):
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
        if background_path:
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
            bg_image_path = _ensure_background(
                style, job_dir,
                lyrics_text=lyrics_text, artist=artist, job_id=job_id,
            )
        update_job(job_id, progress=40)

        files = {}
        chosen_font = None
        bg_source = bg_image_path

        # Step 2 — YouTube lyric video (H.264 / MP4 / 1080p / 24fps)
        if wants_youtube:
            update_job(job_id, current_step="video", progress=40)
            _, chosen_font, bg_source = generate_lyric_video(
                mp3_path, segments, style, job_dir, artist, bg_image_path
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
        if wants_youtube:
            update_job(job_id, current_step="validation", progress=94)
            from content_validator import validate_video as _validate_video
            video_path = os.path.join(job_dir, _DELIVERABLE_FILENAMES["video"])
            validation = _validate_video(video_path, job_id=job_id)
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

        _require_review = os.environ.get("REQUIRE_REVIEW", "true").lower() == "true"
        final_status = "pending_review" if _require_review else "done"

        update_job(job_id, status=final_status, progress=100, files=files)
    except Exception as exc:
        traceback.print_exc()
        update_job(job_id, status="error", error=str(exc))


# ---------------------------------------------------------------------------
# Step 1 — Whisper transcription
# ---------------------------------------------------------------------------

_SPAM_PATTERNS = [
    "suscri", "subscri", "subscribe", "subete", "like", "comment",
    "canal", "channel", "descripci", "description", "link", "follow",
    "sigueme", "intro", "outro", "copyright", "all rights", "derechos",
    "music by", "produced by", "lyrics by", "escucha en", "disponible",
    "spotify", "apple music", "deezer", "itunes", "amazon music",
    "gracias", "thanks for watching", "thanks for listening",
    "subtitulos", "subtitles", "video oficial", "official video",
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


def _transcribe_via_openai_api(mp3_path: str, language: str | None = None) -> list[dict]:
    """Transcribe by calling OpenAI's Whisper API. Returns the same segments
    structure as the local Whisper path. Used in production where loading
    the local model would consume too much worker RAM (~3 GB) and risks OOM.

    Cost: ~$0.006 per minute of audio (~$0.02 per song).

    Why whisper-1 and not gpt-4o-transcribe (better text quality):
        gpt-4o-transcribe and gpt-4o-mini-transcribe only return plain
        text — no segment timestamps. This pipeline renders lyrics
        synchronized to the audio, so segment-level start/end times are
        non-negotiable. whisper-1 (whisper-large-v2) is the only OpenAI
        transcription model that returns verbose_json with segment
        timestamps as of 2026-04. We compensate for its older base model
        by passing initial_prompt and a temperature ladder.
    """
    from openai import OpenAI

    client = OpenAI()  # picks up OPENAI_API_KEY from env
    print(f"[WHISPER-API] transcribing {os.path.basename(mp3_path)} via OpenAI (whisper-1)")

    # Prompt nudges the model to expect song lyrics rather than spoken
    # word — meaningfully improves transcription on heavily-mixed vocals
    # like rock songs. The lyrics token vocabulary it primes is also
    # better for repeated-line detection (chorus).
    kwargs = {
        "model": "whisper-1",
        "response_format": "verbose_json",
        "timestamp_granularities": ["segment"],
        "prompt": "Letras de canción:" if (language or "").startswith("es")
                  else "Song lyrics:",
        # temperature=0 gives the most confident output; we lower the
        # default 0.0 ladder so it doesn't sample alternative
        # interpretations on tricky words.
        "temperature": 0.0,
    }
    if language:
        kwargs["language"] = language

    with open(mp3_path, "rb") as f:
        kwargs["file"] = f
        response = client.audio.transcriptions.create(**kwargs)

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
        if (seg.no_speech_prob or 0) > 0.7:
            print(f"[WHISPER-API] Filtered low-confidence: {text[:60]}")
            continue
        segments.append({
            "start": float(seg.start),
            "end": float(seg.end),
            "text": text,
        })

    GAP = 0.05
    for i in range(len(segments) - 1):
        if segments[i]["end"] > segments[i + 1]["start"] - GAP:
            segments[i]["end"] = segments[i + 1]["start"] - GAP

    print(f"[WHISPER-API] {len(segments)} segments")
    return segments


def transcribe(mp3_path: str, language: str = None) -> list[dict]:
    """Transcribe an audio file to lyric segments.

    Backend selection:
        - If OPENAI_API_KEY is set, route to the OpenAI Whisper API. This is
          the production path: no local model, no OOM risk on 1-2 GB workers.
          Errors propagate — no silent fallback to the 1.5 GB local model
          that frequently OOMs on small instances.
        - If OPENAI_API_KEY is not set, fall back to the local Whisper-turbo
          model. Works for development on machines with enough RAM.
    """
    has_key = bool(os.environ.get("OPENAI_API_KEY", "").strip())
    print(f"[transcribe] OPENAI_API_KEY={'set' if has_key else 'EMPTY'}")
    if has_key:
        return _transcribe_via_openai_api(mp3_path, language=language)

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
# Lyrics reference fetcher — used by /transcribe to show reference text in UI
# ---------------------------------------------------------------------------

_LYRICS_CACHE_DIR = os.path.join(OUTPUTS_DIR, "_lyrics_cache")


def _fetch_lyrics_from_sources(artist: str, song: str) -> list[str]:
    """Fetch reference lyrics for a song from multiple sources.

    Order: (1) local cache, (2) Genius API if GENIUS_TOKEN set. Returns a list
    of strings (longest = most complete). Returns [] on any failure — this is a
    best-effort helper, never raises.
    """
    if not artist or not song:
        return []

    try:
        os.makedirs(_LYRICS_CACHE_DIR, exist_ok=True)
        import hashlib
        key = hashlib.sha1(f"{artist.lower()}|{song.lower()}".encode()).hexdigest()[:16]
        cache_path = os.path.join(_LYRICS_CACHE_DIR, f"{key}.json")
        if os.path.exists(cache_path):
            try:
                with open(cache_path) as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
    except OSError:
        cache_path = None

    results: list[str] = []
    token = os.environ.get("GENIUS_TOKEN", "").strip()
    if token:
        try:
            import lyricsgenius
            g = lyricsgenius.Genius(
                token, timeout=5, retries=1, verbose=False,
                remove_section_headers=True, skip_non_songs=True,
            )
            song_obj = g.search_song(song, artist)
            if song_obj and song_obj.lyrics:
                results.append(song_obj.lyrics)
        except Exception as e:
            print(f"[LYRICS] Genius fetch failed: {e}")

    if cache_path and results:
        try:
            with open(cache_path, "w") as f:
                json.dump(results, f)
        except OSError:
            pass

    return results


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
    """Get a cached Vertex AI GenAI client."""
    global _genai_client
    if _genai_client is None:
        from google import genai
        # Print version once so we can diagnose auth issues that turn out
        # to be SDK-version-specific (Railway sometimes installs stale
        # versions if the requirements pin is too loose).
        print(f"[VERTEX] google-genai version: {genai.__version__}")
        print(f"[VERTEX] project={_VERTEX_PROJECT} location={_VERTEX_LOCATION}")
        print(f"[VERTEX] credentials path: {os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')}")
        print(f"[VERTEX] credentials exists: {os.path.exists(os.environ.get('GOOGLE_APPLICATION_CREDENTIALS', ''))}")
        _genai_client = genai.Client(
            vertexai=True,
            project=_VERTEX_PROJECT,
            location=_VERTEX_LOCATION,
        )
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


def _analyze_lyrics_for_background(lyrics_text: str, artist: str, job_id: str = None) -> dict:
    """Use Gemini to analyze lyrics and choose visual style + prompt.

    Returns dict with:
      - style: "video" | "photo" | "illustration"
      - prompt: the generation prompt for Veo 3 or Imagen 4
    """
    from google import genai
    from provenance import record_ai_call

    client = _get_genai_client()

    system_prompt = """Respond ONLY with a JSON object, no other text. Example:
{"style":"video","prompt":"Slow aerial drone shot over calm ocean at golden sunset, warm cinematic light, 4k"}

"style": always "video"
"prompt": 20-40 word cinematic video scene matching the song's mood and genre. Include camera movement, colors, lighting, atmosphere.

Genre guidance (vary the scenes!):
- Rock/punk → urban, industrial, neon, gritty streets, dark skies, electric storms
- Pop/dance → colorful lights, city nightlife, abstract neon, disco reflections
- Ballad/romantic → sunset, ocean, soft clouds, warm light
- Latin/reggaeton → tropical, vibrant colors, palm trees, warm tones
- Hip hop/rap → city skyline at night, luxury abstract, gold and dark tones

NEVER include people, faces, hands, or text in the prompt."""

    lyrics_sample = lyrics_text[:600]
    # Data minimization (UMG Guideline 14): optionally anonymize artist name
    _send_artist = os.environ.get("SEND_ARTIST_TO_AI", "true").lower() == "true"
    artist_label = artist if _send_artist else "the artist"
    user_content = f"Artist: {artist_label}\n\nLyrics:\n{lyrics_sample}"
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


def _get_unique_prompt(lyrics_text: str = None, artist: str = "", job_id: str = None) -> dict:
    """Get a unique style+prompt combination. Returns {style, prompt}."""
    used: list[str] = []
    if os.path.exists(_USED_PROMPTS_FILE):
        try:
            with open(_USED_PROMPTS_FILE) as f:
                used = json.load(f)
        except (json.JSONDecodeError, OSError):
            used = []

    # Gemini analysis
    if lyrics_text:
        result = _analyze_lyrics_for_background(lyrics_text, artist, job_id=job_id)
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


def _generate_veo_video(prompt: str, output_path: str, job_id: str = None) -> str:
    """Generate a video clip with Google Veo 3. Rate-limit aware."""
    from google import genai
    from google.genai.errors import ClientError
    from provenance import record_ai_call
    import time as _time
    import requests as _req
    global _last_veo_request

    # Proactive cooldown — wait if last request was too recent
    elapsed = _time.time() - _last_veo_request
    if elapsed < _VEO_COOLDOWN and _last_veo_request > 0:
        wait = _VEO_COOLDOWN - elapsed
        print(f"[BG] Cooldown: waiting {wait:.0f}s before next Veo request...")
        _time.sleep(wait)

    client = _get_genai_client()

    safe_prompt = f"{prompt}. Photorealistic, filmed with cinema camera, real footage. No text, no words, no letters, no people, no faces, no hands, no CGI, no animation."

    recorder = record_ai_call(
        job_id=job_id or "unknown",
        step="video_bg",
        tool_name="veo-3.1-generate-001",
        tool_provider="google_vertex",
        prompt=safe_prompt,
        input_data_types=["generated_prompt"],
    ) if job_id else None

    # Patient retries for batch processing — wait and retry up to 5 times
    for attempt in range(5):
        try:
            print(f"[BG] Veo 3: generating video (attempt {attempt + 1}/5)...")
            operation = client.models.generate_videos(
                model="veo-3.1-generate-001",
                prompt=safe_prompt,
                config=genai.types.GenerateVideosConfig(
                    aspect_ratio="16:9",
                    number_of_videos=1,
                    generate_audio=False,
                ),
            )
            break
        except ClientError as e:
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                wait = 60 * (attempt + 1)  # 60s, 120s, 180s, 240s, 300s
                print(f"[BG] Rate limited, waiting {wait}s before retry...")
                _time.sleep(wait)
            else:
                if recorder:
                    recorder.finish(response_summary=f"error: {str(e)[:200]}")
                raise
    else:
        if recorder:
            recorder.finish(response_summary="error: rate_limit_exceeded_after_5_retries")
        raise RuntimeError("Veo 3 rate limit exceeded after 5 retries (~15 min wait)")

    _last_veo_request = _time.time()

    # Poll with a hard 10-min cap so a stuck operation never hangs a worker.
    poll_deadline = _time.time() + 600
    while not operation.done:
        if _time.time() > poll_deadline:
            raise TimeoutError("Veo 3 operation timed out after 10 min")
        _time.sleep(10)
        operation = client.operations.get(operation)

    video = operation.result.generated_videos[0]
    # Download via authenticated request (Vertex AI)
    import google.auth
    import google.auth.transport.requests
    credentials, _ = google.auth.default()
    credentials.refresh(google.auth.transport.requests.Request())
    resp = _req.get(
        video.video.uri,
        headers={"Authorization": f"Bearer {credentials.token}"},
    )
    resp.raise_for_status()
    with open(output_path, "wb") as f:
        f.write(resp.content)

    size_mb = os.path.getsize(output_path) / 1024 / 1024
    print(f"[BG] Veo 3 video saved: {size_mb:.1f} MB")
    if recorder:
        recorder.finish(
            response_summary=f"video_generated: {size_mb:.1f}MB",
            output_artifact=output_path,
        )
    return output_path


def _generate_imagen_image(prompt: str, output_path: str, max_retries: int = 5, job_id: str = None) -> str:
    """Generate an image with Google Imagen 4. Auto-retries on rate limit."""
    from google import genai
    from google.genai.errors import ClientError
    from provenance import record_ai_call
    import time as _time

    client = _get_genai_client()

    safe_prompt = f"{prompt}. No text, no words, no letters, no people, no faces, no hands."

    recorder = record_ai_call(
        job_id=job_id or "unknown",
        step="image_bg",
        tool_name="imagen-4.0-generate-001",
        tool_provider="google_vertex",
        prompt=safe_prompt,
        input_data_types=["generated_prompt"],
    ) if job_id else None

    for attempt in range(max_retries):
        try:
            print(f"[BG] Imagen 4: generating image (attempt {attempt + 1})...")
            response = client.models.generate_images(
                model="imagen-4.0-generate-001",
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


def _ensure_background(style_hint: str, job_dir: str, lyrics_text: str = None, artist: str = "", job_id: str = None) -> str:
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
    result = _get_unique_prompt(lyrics_text, artist, job_id=job_id)
    prompt = result["prompt"]

    bg_path = os.path.join(job_dir, "bg_generated.mp4")
    import time as _time_bg
    for attempt in range(3):
        try:
            _generate_veo_video(prompt, bg_path, job_id=job_id)
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

    # Show artist + song title during instrumental intro
    first_lyric_start = segments[0]["start"] if segments else duration
    if first_lyric_start > 3 and artist:
        raw_name = os.path.splitext(os.path.basename(mp3_path))[0]
        title_song = raw_name
        if " - " in raw_name:
            title_song = raw_name.split(" - ", 1)[1]
        for sfx in ["(Official Video)", "(Official Audio)", "(Lyric Video)",
                     "(Official Music Video)", "(Audio)", "(Video)", "(En Vivo)",
                     "(Live)", "(Lyrics)"]:
            title_song = title_song.replace(sfx, "").strip()
        title_end = first_lyric_start - 0.5
        title_layers = _make_text_clip(
            f"{artist}\n{title_song}", 0.5, title_end, font, spec=spec
        )
        text_layers.extend(title_layers)

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
            "-ar", "48000",
        ]
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
    """Create text clips sized for vertical 1080x1920 short."""
    display_text = text.upper()

    text_len = len(display_text)
    if text_len > 60:
        fontsize = 40
        text_width = 950
    elif text_len > 35:
        fontsize = 50
        text_width = 900
    else:
        fontsize = 65
        text_width = 850

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
    try:
        font_artist = ImageFont.truetype(thumb_font, 100)
        font_song = ImageFont.truetype(thumb_font, 55)
    except (OSError, IOError):
        font_artist = ImageFont.load_default()
        font_song = ImageFont.load_default()

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
