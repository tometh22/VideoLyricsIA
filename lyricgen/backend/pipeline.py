"""Full processing pipeline: Whisper → Video → Short → Thumbnail."""

import json
import os
import math
import random
import subprocess
import tempfile
import traceback

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

from jobs import update_job

ASSETS_DIR = os.path.join(os.path.dirname(__file__), "..", "assets")
OUTPUTS_DIR = os.path.join(os.path.dirname(__file__), "..", "outputs")
BACKGROUNDS_DIR = os.path.join(ASSETS_DIR, "backgrounds")


def run_pipeline(job_id: str, mp3_path: str, artist: str, style: str,
                 language: str = None, segments_override: list[dict] = None):
    """Run the full pipeline for a job. Called synchronously."""
    job_dir = os.path.join(OUTPUTS_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    try:
        # Step 1 — Whisper transcription (or use edited segments)
        update_job(job_id, current_step="whisper", progress=5)
        if segments_override:
            segments = segments_override
            print(f"[WHISPER] Using {len(segments)} user-edited segments")
        else:
            segments = transcribe(mp3_path, language=language)
        update_job(job_id, progress=20)

        # Step 1.5 — Generate AI background (Veo 3 with lyrics analysis)
        update_job(job_id, current_step="background", progress=22)
        lyrics_text = " ".join(seg["text"] for seg in segments)
        bg_image_path = _ensure_background(style, job_dir, lyrics_text=lyrics_text, artist=artist)
        update_job(job_id, progress=40)

        # Step 2 — Full lyric video
        update_job(job_id, current_step="video", progress=42)
        video_path, chosen_font, bg_source = generate_lyric_video(
            mp3_path, segments, style, job_dir, artist, bg_image_path
        )
        update_job(job_id, progress=65)

        # Step 3 — YouTube Short (uses raw background, not lyric video)
        update_job(job_id, current_step="short", progress=68)
        short_path = generate_short(
            mp3_path, segments, job_dir, bg_source=bg_source,
            style=style, font=chosen_font,
        )
        update_job(job_id, progress=85)

        # Step 4 — Thumbnail (uses raw background, not lyric video)
        update_job(job_id, current_step="thumbnail", progress=90)
        thumb_path = generate_thumbnail(
            artist, mp3_path, job_dir, bg_source=bg_source,
        )
        update_job(job_id, progress=100)

        update_job(
            job_id,
            status="done",
            files={
                "video_url": f"/download/{job_id}/video",
                "short_url": f"/download/{job_id}/short",
                "thumbnail_url": f"/download/{job_id}/thumbnail",
            },
        )
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


def transcribe(mp3_path: str, language: str = None) -> list[dict]:
    """Transcribe: isolate vocals with Demucs, then transcribe with Whisper turbo."""
    import whisper

    # Use original audio — Demucs vocal isolation can introduce artifacts
    # that confuse Whisper more than the original mix
    audio_path = mp3_path

    # Transcribe with Whisper turbo
    model = whisper.load_model("turbo")

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

    for i, seg in enumerate(segments[:5]):
        print(f"[WHISPER] seg {i}: {seg['start']:.2f}–{seg['end']:.2f}  {seg['text'][:60]}")

    GAP = 0.05
    for i in range(len(segments) - 1):
        if segments[i]["end"] > segments[i + 1]["start"] - GAP:
            segments[i]["end"] = segments[i + 1]["start"] - GAP

    return segments


# ---------------------------------------------------------------------------
# Step 1.5 — AI Background Generation (Google Veo 3 → SD fallback)
# ---------------------------------------------------------------------------

_VERTEX_CREDENTIALS = os.path.join(os.path.dirname(__file__), "vertex_credentials.json")
_VERTEX_PROJECT = "gen-lang-client-0900526123"
_VERTEX_LOCATION = "us-central1"

# Set credentials env var for Google SDK
os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", _VERTEX_CREDENTIALS)

_genai_client = None


def _get_genai_client():
    """Get a cached Vertex AI GenAI client."""
    global _genai_client
    if _genai_client is None:
        from google import genai
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


def _analyze_lyrics_for_background(lyrics_text: str, artist: str) -> dict:
    """Use Gemini to analyze lyrics and choose visual style + prompt.

    Returns dict with:
      - style: "video" | "photo" | "illustration"
      - prompt: the generation prompt for Veo 3 or Imagen 4
    """
    from google import genai

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

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=f"Artist: {artist}\n\nLyrics:\n{lyrics_sample}",
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
                    return {"style": style, "prompt": prompt}
            except json.JSONDecodeError:
                pass

        print("[BG] Failed to parse Gemini JSON, using combinatorial fallback")
        return {"style": "video", "prompt": None}

    except Exception as e:
        print(f"[BG] Gemini analysis failed: {e}, using video fallback")
        return {"style": "video", "prompt": None}


def _get_unique_prompt(lyrics_text: str = None, artist: str = "") -> dict:
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
        result = _analyze_lyrics_for_background(lyrics_text, artist)
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


def _generate_veo_video(prompt: str, output_path: str) -> str:
    """Generate a video clip with Google Veo 3. Fast fail on rate limit."""
    from google import genai
    from google.genai.errors import ClientError
    import time as _time
    import requests as _req

    client = _get_genai_client()

    safe_prompt = f"{prompt}. Photorealistic, filmed with cinema camera, real footage. No text, no words, no letters, no people, no faces, no hands, no CGI, no animation."

    # Only 2 quick retries — if rate limited, fall back to Imagen 4 fast
    for attempt in range(2):
        try:
            print(f"[BG] Veo 3: generating video (attempt {attempt + 1})...")
            operation = client.models.generate_videos(
                model="veo-3.0-generate-001",
                prompt=safe_prompt,
                config=genai.types.GenerateVideosConfig(
                    aspect_ratio="16:9",
                    number_of_videos=1,
                ),
            )
            break
        except ClientError as e:
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                if attempt == 0:
                    print("[BG] Rate limited, waiting 30s...")
                    _time.sleep(30)
                else:
                    raise RuntimeError("Veo 3 rate limited — switching to Imagen 4")
            else:
                raise
    else:
        raise RuntimeError("Veo 3 rate limited — switching to Imagen 4")

    while not operation.done:
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

    print(f"[BG] Veo 3 video saved: {os.path.getsize(output_path)/1024/1024:.1f} MB")
    return output_path


def _generate_imagen_image(prompt: str, output_path: str, max_retries: int = 5) -> str:
    """Generate an image with Google Imagen 4. Auto-retries on rate limit."""
    from google import genai
    from google.genai.errors import ClientError
    import time as _time

    client = _get_genai_client()

    safe_prompt = f"{prompt}. No text, no words, no letters, no people, no faces, no hands."

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
                raise
    else:
        raise RuntimeError("Imagen 4 rate limit exceeded after all retries")

    image = response.generated_images[0]
    # Save image bytes
    img_bytes = image.image.image_bytes
    with open(output_path, "wb") as f:
        f.write(img_bytes)

    print(f"[BG] Imagen 4 saved: {os.path.getsize(output_path)/1024:.0f} KB")
    return output_path


def _ensure_background(style_hint: str, job_dir: str, lyrics_text: str = None, artist: str = "") -> str:
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
    result = _get_unique_prompt(lyrics_text, artist)
    prompt = result["prompt"]

    bg_path = os.path.join(job_dir, "bg_generated.mp4")
    import time as _time_bg
    for attempt in range(2):
        try:
            _generate_veo_video(prompt, bg_path)
            return bg_path
        except Exception as e:
            print(f"[BG] Veo 3 attempt {attempt + 1} failed: {e}")
            if attempt == 0:
                _time_bg.sleep(5)

    # All Veo attempts failed — render a gradient as fallback
    print("[BG] Veo 3 unavailable, falling back to gradient background")
    fallback_path = os.path.join(job_dir, "bg_gradient_fallback.mp4")
    gradient = _make_gradient_clip(30.0, style_hint)
    gradient.write_videofile(fallback_path, fps=24, logger=None)
    gradient.close()
    return fallback_path


def _ken_burns_clip(image_path: str, duration: float):
    """Create an animated Ken Burns clip with periodic direction changes."""
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
            Image.fromarray(crop).resize((1920, 1080), Image.LANCZOS)
        )
        return resized

    return VideoClip(make_frame, duration=duration).set_fps(24)


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


def _make_gradient_clip(duration: float, style: str = "oscuro"):
    """Generate a cinematic animated gradient as fallback background."""
    palette = _GRADIENT_PALETTES.get(style, _GRADIENT_PALETTES["oscuro"])
    top = np.array(palette[0], dtype=np.float64)
    mid1 = np.array(palette[1], dtype=np.float64)
    mid2 = np.array(palette[2], dtype=np.float64)
    bot = np.array(palette[3], dtype=np.float64)

    _rows = np.zeros((1080, 1920, 3), dtype=np.float64)
    for y in range(1080):
        ratio = y / 1080
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

    return VideoClip(_gradient_frame, duration=duration).set_fps(24)


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


def _prerender_looped_bg(bg_path: str, duration: float, job_dir: str, target_w=1920, target_h=1080) -> str:
    """Pre-render a seamlessly looped background video using ffmpeg.

    Uses ffmpeg's native looping + scale/crop (much faster and smoother than
    Python frame-by-frame). The result is a fluent, high-fps MP4 file.
    """
    out_path = os.path.join(job_dir, "bg_looped.mp4")

    # ffmpeg: loop input, scale to cover target, center crop, trim to duration
    # -stream_loop -1 = infinite loop; we trim with -t
    cmd = [
        "ffmpeg", "-y",
        "-stream_loop", "-1",
        "-i", bg_path,
        "-t", str(duration),
        "-vf", (
            f"scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
            f"crop={target_w}:{target_h},"
            "setpts=PTS-STARTPTS"
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
        raise RuntimeError(f"ffmpeg loop failed: {result.stderr[-300:]}")

    size_mb = os.path.getsize(out_path) / 1024 / 1024
    print(f"[BG] Pre-rendered loop: {duration:.0f}s, {size_mb:.1f} MB")
    return out_path


def _get_background_clip_from_path(bg_path: str, style: str, duration: float, job_dir: str = None):
    """Load a background video, loop it seamlessly via ffmpeg, return clip."""
    try:
        clip = VideoFileClip(bg_path)
        clip.get_frame(0)
        clip_dur = clip.duration
        clip.close()
    except Exception as e:
        raise RuntimeError(f"Cannot load background video: {e}")

    if clip_dur >= duration:
        return _cover_resize(VideoFileClip(bg_path)).subclip(0, duration)

    # Pre-render the looped video with ffmpeg (fast, fluid, native fps)
    if job_dir:
        looped_path = _prerender_looped_bg(bg_path, duration, job_dir)
        return VideoFileClip(looped_path)

    # Fallback: simple concatenation
    loops = math.ceil(duration / clip_dur) + 1
    clips = [_cover_resize(VideoFileClip(bg_path)) for _ in range(loops)]
    return concatenate_videoclips(clips).subclip(0, duration)


# Font pool — Google Fonts only (SIL OFL = full commercial use, no royalties)
_FONTS_DIR = os.path.join(os.path.dirname(__file__), "..", "assets", "fonts")
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


def _make_text_clip(text: str, seg_start: float, seg_end: float, font: str = "Arial"):
    """Create a clean text clip matching pro lyric video style (bold white, subtle shadow)."""
    import unicodedata
    # Sanitize text: normalize unicode and remove problematic characters
    display_text = unicodedata.normalize("NFC", text.upper())
    # Remove characters that break ImageMagick's @file parsing
    display_text = display_text.replace("@", "").replace("`", "'").replace("\x00", "")

    text_len = len(display_text)
    if text_len > 80:
        fontsize = 55
        text_width = 1700
    elif text_len > 50:
        fontsize = 70
        text_width = 1650
    else:
        fontsize = 85
        text_width = 1500

    # Fallback font if the selected one fails with ImageMagick
    fallback_font = os.path.join(_FONTS_DIR, "Montserrat-Bold.ttf")

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
    shadow_y = (1080 - sh) // 2 + 3
    shadow_x = (1920 - text_width) // 2 + 3
    shadow = shadow.set_position((shadow_x, shadow_y)).set_start(seg_start).set_end(seg_end)

    # Main text — clean white, thin stroke
    txt = _try_text_clip(display_text, fontsize, font, "white",
                         stroke_color="black", stroke_width=1.5
    ).set_position("center").set_start(seg_start).set_end(seg_end)

    return [shadow, txt]


def generate_lyric_video(
    mp3_path: str,
    segments: list[dict],
    style: str,
    job_dir: str,
    artist: str,
    bg_image_path: str | None = None,
) -> tuple[str, str, str | None]:
    """Generate a 1920x1080 lyric video. Returns (video_path, font, bg_source).

    bg_source is the path to the raw background (mp4 or jpg) so short/thumbnail
    can use it without burned-in lyrics.
    """
    audio = AudioFileClip(mp3_path)
    duration = audio.duration

    # Load background — can be video (.mp4) or image (.jpg/.png with Ken Burns)
    bg_source = bg_image_path
    if not bg_source:
        bg_source = _find_background_video()
    if not bg_source:
        raise RuntimeError("No background available. Check Veo 3 API or add videos to assets/backgrounds/")

    if bg_source.lower().endswith((".jpg", ".jpeg", ".png")):
        bg = _ken_burns_clip(bg_source, duration)
    else:
        bg = _get_background_clip_from_path(bg_source, style, duration, job_dir)

    # Pick a random font for this job
    font = random.choice(_FONT_POOL) if _FONT_POOL else "Arial"
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
            f"{artist}\n{title_song}", 0.5, title_end, font
        )
        text_layers.extend(title_layers)

    for seg in segments:
        layers = _make_text_clip(seg["text"], seg["start"], seg["end"], font)
        text_layers.extend(layers)

    video = CompositeVideoClip([bg] + text_layers, size=(1920, 1080))
    video = video.set_audio(audio).set_duration(duration)

    out_path = os.path.join(job_dir, "lyric_video.mp4")
    video.write_videofile(
        out_path,
        fps=24,
        codec="libx264",
        audio_codec="aac",
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
