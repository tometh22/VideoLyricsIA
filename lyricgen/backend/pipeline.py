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


def run_pipeline(job_id: str, mp3_path: str, artist: str, style: str):
    """Run the full pipeline for a job. Called synchronously."""
    job_dir = os.path.join(OUTPUTS_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    try:
        # Step 1 — Whisper transcription
        update_job(job_id, current_step="whisper", progress=5)
        segments = transcribe(mp3_path)
        update_job(job_id, progress=20)

        # Step 1.5 — Generate AI background if no video files available
        update_job(job_id, current_step="background", progress=22)
        bg_image_path = _ensure_background(style, job_dir)
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

def transcribe(mp3_path: str) -> list[dict]:
    """Transcribe an MP3 using local openai-whisper and return segments."""
    import whisper

    model = whisper.load_model("small")

    # initial_prompt="Lyrics:" prevents Whisper from ignoring early vocals
    # condition_on_previous_text=False avoids hallucination cascading
    result = model.transcribe(
        mp3_path,
        word_timestamps=True,
        initial_prompt="Lyrics:",
        condition_on_previous_text=False,
    )
    segments = [
        {"start": seg["start"], "end": seg["end"], "text": seg["text"].strip()}
        for seg in result["segments"]
        if seg["text"].strip()
    ]

    # Safety net: if first segment starts very late (>30s), retry without
    # condition_on_previous_text=False in case it helps
    if segments and segments[0]["start"] > 30:
        print(f"[WHISPER] WARNING: first segment at {segments[0]['start']:.1f}s, retrying with fallback settings")
        result2 = model.transcribe(
            mp3_path,
            word_timestamps=True,
            initial_prompt="Song lyrics transcription:",
            no_speech_threshold=0.4,
        )
        segments2 = [
            {"start": seg["start"], "end": seg["end"], "text": seg["text"].strip()}
            for seg in result2["segments"]
            if seg["text"].strip()
        ]
        if segments2 and segments2[0]["start"] < segments[0]["start"]:
            print(f"[WHISPER] Retry found earlier lyrics at {segments2[0]['start']:.1f}s, using retry result")
            segments = segments2

    # Log first 5 segments for debug
    for i, seg in enumerate(segments[:5]):
        print(f"[WHISPER] seg {i}: {seg['start']:.2f}–{seg['end']:.2f}  {seg['text'][:60]}")

    # Fix overlapping segments: ensure seg[i].end <= seg[i+1].start
    GAP = 0.05  # 50ms gap between segments to prevent visual overlap
    for i in range(len(segments) - 1):
        if segments[i]["end"] > segments[i + 1]["start"] - GAP:
            segments[i]["end"] = segments[i + 1]["start"] - GAP

    return segments


# ---------------------------------------------------------------------------
# Step 1.5 — AI Background Generation (Stable Diffusion)
# ---------------------------------------------------------------------------

_STYLE_PROMPTS = {
    "oscuro": [
        "vibrant purple and blue galaxy nebula with bright stars, colorful space, 4k wallpaper",
        "colorful northern lights aurora over snowy mountains, vivid green and purple sky, 4k",
        "dramatic sunset with vibrant orange purple clouds over city skyline, colorful, 4k",
        "colorful abstract liquid art, swirling purple blue and pink paint, vibrant, 4k",
        "underwater bioluminescent ocean scene, glowing jellyfish, vibrant blue and purple, 4k",
    ],
    "neon": [
        "vibrant neon city street at night, pink blue purple lights everywhere, colorful reflections, 4k",
        "colorful neon signs and lights in rain, cyberpunk city, vivid pink cyan magenta, 4k",
        "bright neon geometric shapes floating in space, colorful abstract, pink blue green, 4k",
        "futuristic neon tunnel with rainbow lights, vibrant colorful, 4k wallpaper",
        "neon-lit japanese street with cherry blossoms, vibrant pink and blue lights, colorful, 4k",
    ],
    "minimal": [
        "beautiful pastel gradient sky with soft pink orange and lavender clouds, dreamy, 4k",
        "colorful abstract watercolor wash, soft pink blue and gold blending, artistic, 4k",
        "bright sunny sky with fluffy white clouds, cheerful vibrant blue, 4k wallpaper",
        "soft holographic rainbow gradient, iridescent pastel colors, beautiful, 4k",
        "cherry blossom tree with soft pink petals floating, bright spring day, beautiful, 4k",
    ],
    "calido": [
        "stunning tropical sunset over turquoise ocean, vibrant orange pink sky, palm trees, 4k",
        "colorful hot air balloons floating over green valley at golden hour, vibrant, 4k",
        "bright sunflower field under vivid blue sky with golden sunlight, cheerful, 4k",
        "tropical paradise beach with crystal clear water, vibrant turquoise and golden sand, 4k",
        "colorful autumn forest with bright red orange yellow leaves, golden sunlight, 4k",
    ],
}

_sd_pipe = None


def _get_sd_pipeline():
    """Load Stable Diffusion pipeline (cached). Uses MPS on Apple Silicon."""
    global _sd_pipe
    if _sd_pipe is not None:
        return _sd_pipe

    import torch
    from diffusers import StableDiffusionPipeline

    model_id = "runwayml/stable-diffusion-v1-5"

    if torch.backends.mps.is_available():
        device = "mps"
        dtype = torch.float32  # float16 produces NaN/black images on MPS
    elif torch.cuda.is_available():
        device = "cuda"
        dtype = torch.float16
    else:
        device = "cpu"
        dtype = torch.float32

    _sd_pipe = StableDiffusionPipeline.from_pretrained(
        model_id,
        torch_dtype=dtype,
        safety_checker=None,
    )
    _sd_pipe = _sd_pipe.to(device)
    if hasattr(_sd_pipe, "enable_attention_slicing"):
        _sd_pipe.enable_attention_slicing()

    return _sd_pipe


def _generate_ai_background(style: str, output_path: str) -> str:
    """Generate a unique background image using Stable Diffusion."""
    prompts = _STYLE_PROMPTS.get(style, _STYLE_PROMPTS["oscuro"])
    prompt = random.choice(prompts)

    pipe = _get_sd_pipeline()

    image = pipe(
        prompt,
        negative_prompt="text, watermark, logo, words, letters, blurry, low quality",
        width=768,
        height=512,
        num_inference_steps=25,
        guidance_scale=7.5,
    ).images[0]

    # Validate the image is not blank/black (SD can fail silently on MPS)
    arr = np.array(image)
    if arr.mean() < 5:
        raise RuntimeError("Stable Diffusion produced a blank/black image")

    image = image.resize((1920, 1080), Image.LANCZOS)
    image.save(output_path, "JPEG", quality=95)
    return output_path


def _ensure_background(style: str, job_dir: str) -> str | None:
    """Generate AI background if no video files exist. Safe fallback on failure."""
    # If there are any video files in backgrounds dir, prefer those
    all_videos = []
    if os.path.isdir(BACKGROUNDS_DIR):
        for root, _, files in os.walk(BACKGROUNDS_DIR):
            all_videos.extend(f for f in files if f.lower().endswith(".mp4"))
    if all_videos:
        return None

    # No videos — try Stable Diffusion, but don't crash if it fails
    try:
        bg_path = os.path.join(job_dir, "ai_background.jpg")
        _generate_ai_background(style, bg_path)
        return bg_path
    except Exception as e:
        print(f"[WARNING] Stable Diffusion failed, using gradient fallback: {e}")
        return None


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


def _get_background_clip_from_path(bg_path: str, style: str, duration: float):
    """Load a specific background video, resize and loop it."""
    try:
        clip = VideoFileClip(bg_path)
        clip.get_frame(0)  # validate it's readable
        clip = _cover_resize(clip)
    except Exception as e:
        print(f"[BG] Failed to load {bg_path}: {e}, using gradient fallback")
        return _make_gradient_clip(duration, style)
    if clip.duration >= duration:
        return clip.subclip(0, duration)

    # Loop with fade-out/fade-in at each seam to hide the cut.
    # Each iteration opens a fresh VideoFileClip to avoid shared ffmpeg reader bugs.
    fade_dur = min(1.0, clip.duration * 0.2)
    clip_dur = clip.duration
    clip.close()
    loops_needed = math.ceil(duration / clip_dur) + 1
    clips = []
    for i in range(loops_needed):
        c = _cover_resize(VideoFileClip(bg_path))
        c = c.fadeout(fade_dur)
        if i > 0:
            c = c.fadein(fade_dur)
        clips.append(c)
    looped = concatenate_videoclips(clips)
    return looped.subclip(0, duration)


# Font pool — professional, legible bold fonts only
_FONT_POOL = [fp for fp in [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Rounded Bold.ttf",
    "/System/Library/Fonts/Supplemental/DIN Alternate Bold.ttf",
    "/System/Library/Fonts/Supplemental/DIN Condensed Bold.ttf",
    "/System/Library/Fonts/Supplemental/Verdana Bold.ttf",
    "/System/Library/Fonts/Supplemental/Trebuchet MS Bold.ttf",
    "/System/Library/Fonts/Supplemental/Impact.ttf",
    "/System/Library/Fonts/Supplemental/Tahoma Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
] if os.path.exists(fp)]


def _make_text_clip(text: str, seg_start: float, seg_end: float, font: str = "Arial"):
    """Create a text clip with shadow for one lyric segment."""
    display_text = text.upper()

    # Reduce font size for long lines to prevent text clipping
    text_len = len(display_text)
    if text_len > 80:
        fontsize = 55
        text_width = 1700
    elif text_len > 50:
        fontsize = 70
        text_width = 1650
    else:
        fontsize = 90
        text_width = 1500

    # Shadow layer
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
    shadow_y = (1080 - sh) // 2 + 4
    shadow_x = (1920 - text_width) // 2 + 4
    shadow = shadow.set_position((shadow_x, shadow_y)).set_start(seg_start).set_end(seg_end)

    # Main text layer
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

    # Use AI-generated image with Ken Burns if available
    bg_source = None  # raw background path for reuse by short/thumbnail
    if bg_image_path and os.path.exists(bg_image_path):
        bg = _ken_burns_clip(bg_image_path, duration)
        bg_source = bg_image_path
    else:
        bg_source = _find_background_video()
        if bg_source:
            bg = _get_background_clip_from_path(bg_source, style, duration)
        else:
            bg = _make_gradient_clip(duration, style)

    # Pick a random font for this job
    font = random.choice(_FONT_POOL) if _FONT_POOL else "Arial"
    print(f"[FONT] Selected: {os.path.basename(font)}")

    # Build text clips — each segment gets its own shadow + text
    text_layers = []

    # Show artist + song title during instrumental intro
    first_lyric_start = segments[0]["start"] if segments else duration
    if first_lyric_start > 3 and artist:
        song_name = os.path.splitext(os.path.basename(mp3_path))[0]
        title_end = first_lyric_start - 0.5
        title_layers = _make_text_clip(
            f"{artist}\n{song_name}", 0.5, title_end, font
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
    song_name = os.path.splitext(os.path.basename(mp3_path))[0]

    font_paths = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    font_artist = ImageFont.load_default()
    font_song = ImageFont.load_default()
    for fp in font_paths:
        if os.path.exists(fp):
            try:
                font_artist = ImageFont.truetype(fp, 100)
                font_song = ImageFont.truetype(fp, 55)
                break
            except (OSError, IOError):
                continue

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
