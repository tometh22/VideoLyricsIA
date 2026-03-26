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
        video_path = generate_lyric_video(
            mp3_path, segments, style, job_dir, artist, bg_image_path
        )
        update_job(job_id, progress=65)

        # Step 3 — YouTube Short
        update_job(job_id, current_step="short", progress=68)
        short_path = generate_short(mp3_path, video_path, segments, job_dir)
        update_job(job_id, progress=85)

        # Step 4 — Thumbnail
        update_job(job_id, current_step="thumbnail", progress=90)
        thumb_path = generate_thumbnail(video_path, artist, mp3_path, job_dir)
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
    result = model.transcribe(mp3_path)
    segments = [
        {"start": seg["start"], "end": seg["end"], "text": seg["text"].strip()}
        for seg in result["segments"]
    ]
    return segments


# ---------------------------------------------------------------------------
# Step 1.5 — AI Background Generation (Stable Diffusion)
# ---------------------------------------------------------------------------

_STYLE_PROMPTS = {
    "oscuro": [
        "cinematic dark city skyline at night, moody atmosphere, rain reflections, 4k",
        "dark ocean waves at night, moonlight reflections, cinematic, moody",
        "dark forest with fog and moonlight, cinematic atmosphere, 4k",
        "abandoned neon-lit alley at night, rain puddles, cinematic, dark",
        "dark mountains with aurora borealis, cinematic landscape, 4k",
    ],
    "neon": [
        "neon-lit cyberpunk city street, rain reflections, pink and blue lights, 4k",
        "neon signs glowing in dark alley, purple and cyan lights, cinematic",
        "futuristic neon cityscape, holographic lights, cyberpunk, 4k",
        "neon tunnel with colorful lights, futuristic, cinematic atmosphere",
        "neon-lit japanese street at night, rain, pink and blue, cinematic",
    ],
    "minimal": [
        "minimalist abstract gradient, soft pastel colors, clean, 4k",
        "white marble texture with soft lighting, minimal, elegant, 4k",
        "soft clouds in pale sky, minimalist, clean atmosphere, 4k",
        "abstract soft geometric shapes, minimal, muted earth tones, 4k",
        "calm still water with soft sky reflection, minimal, serene, 4k",
    ],
    "calido": [
        "tropical beach sunset with palm trees silhouette, warm golden light, cinematic",
        "golden hour sunlight through autumn trees, warm tones, cinematic, 4k",
        "sunset over calm ocean, warm orange and pink sky, cinematic",
        "desert landscape at golden hour, warm sand dunes, cinematic, 4k",
        "cozy warm candlelight bokeh, soft amber tones, cinematic",
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
        dtype = torch.float16
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

    image = image.resize((1920, 1080), Image.LANCZOS)
    image.save(output_path, "JPEG", quality=95)
    return output_path


def _ensure_background(style: str, job_dir: str) -> str | None:
    """Generate AI background if no video files exist. Safe fallback on failure."""
    # If there are video files, prefer those
    if _find_background_video(style) is not None:
        return None
    if _find_background_video("oscuro") is not None:
        return None

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
    """Create an animated Ken Burns (slow zoom + pan) clip from a still image."""
    img = np.array(Image.open(image_path))
    h, w = img.shape[:2]

    zoom_in = random.choice([True, False])
    pan_x = random.uniform(-0.05, 0.05)
    pan_y = random.uniform(-0.03, 0.03)

    def make_frame(t):
        progress = t / max(duration, 1)
        if zoom_in:
            scale = 1.0 + 0.15 * progress
        else:
            scale = 1.15 - 0.15 * progress

        cw = int(w / scale)
        ch = int(h / scale)
        cx = int((w - cw) / 2 + pan_x * progress * w)
        cy = int((h - ch) / 2 + pan_y * progress * h)
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

def _find_background_video(style: str) -> str | None:
    """Find a random background video for the given style."""
    candidates: list[str] = []

    style_dir = os.path.join(BACKGROUNDS_DIR, style)
    if os.path.isdir(style_dir):
        candidates.extend(
            os.path.join(style_dir, f)
            for f in os.listdir(style_dir)
            if f.lower().endswith(".mp4")
        )

    if os.path.isdir(BACKGROUNDS_DIR):
        for f in os.listdir(BACKGROUNDS_DIR):
            if not f.lower().endswith(".mp4"):
                continue
            name = os.path.splitext(f)[0]
            if name == style or name.startswith(f"{style}_"):
                candidates.append(os.path.join(BACKGROUNDS_DIR, f))

    if candidates:
        return random.choice(candidates)
    return None


def _make_gradient_clip(duration: float):
    """Generate a cinematic animated gradient as fallback background."""
    _rows = np.zeros((1080, 1920, 3), dtype=np.float64)
    for y in range(1080):
        ratio = y / 1080
        if ratio < 0.4:
            r, g, b = 15 + 40 * (ratio / 0.4), 20 + 30 * (ratio / 0.4), 50 + 40 * (ratio / 0.4)
        elif ratio < 0.65:
            p = (ratio - 0.4) / 0.25
            r, g, b = 55 + 140 * p, 50 - 20 * p, 90 - 30 * p
        else:
            p = (ratio - 0.65) / 0.35
            r, g, b = 195 - 170 * p, 30 - 20 * p, 60 - 40 * p
        _rows[y, :] = [r, g, b]

    def _gradient_frame(t):
        shift = 15 * np.sin(t * 0.15)
        frame = _rows.copy()
        frame[:, :, 0] = np.clip(frame[:, :, 0] + shift, 0, 255)
        frame[:, :, 2] = np.clip(frame[:, :, 2] - shift * 0.5, 0, 255)
        return frame.astype(np.uint8)

    return VideoClip(_gradient_frame, duration=duration).set_fps(24)


def _get_background_clip(style: str, duration: float):
    """Load and loop a random background video for the given style."""
    bg_path = _find_background_video(style)
    if bg_path is None:
        bg_path = _find_background_video("oscuro")
    if bg_path is None:
        all_videos = []
        if os.path.isdir(BACKGROUNDS_DIR):
            for root, _, files in os.walk(BACKGROUNDS_DIR):
                all_videos.extend(
                    os.path.join(root, f)
                    for f in files if f.lower().endswith(".mp4")
                )
        if all_videos:
            bg_path = random.choice(all_videos)

    if bg_path is None:
        return _make_gradient_clip(duration)

    clip = VideoFileClip(bg_path)
    if clip.duration >= duration:
        return clip.subclip(0, duration)
    loops_needed = math.ceil(duration / clip.duration)
    looped = concatenate_videoclips([clip] * loops_needed)
    return looped.subclip(0, duration)


# Font detection
_FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Bold Italic.ttf",
    "/System/Library/Fonts/Supplemental/Impact.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
_LYRIC_FONT = None
for _fp in _FONT_CANDIDATES:
    if os.path.exists(_fp):
        _LYRIC_FONT = _fp
        break


def _make_text_clip(text: str, seg_start: float, seg_end: float):
    """Create a text clip with shadow for one lyric segment.

    Uses default args to capture seg_start/seg_end by value (not reference).
    """
    display_text = text.upper()
    font = _LYRIC_FONT or "Arial"

    # Shadow layer
    shadow = TextClip(
        display_text,
        fontsize=90,
        font=font,
        color="black",
        method="caption",
        size=(1500, None),
        align="center",
    ).set_opacity(0.6)

    # Get shadow height to position it slightly below center
    sh = shadow.size[1]
    shadow_y = (1080 - sh) // 2 + 4
    shadow_x = (1920 - 1500) // 2 + 4
    shadow = shadow.set_position((shadow_x, shadow_y)).set_start(seg_start).set_end(seg_end)

    # Main text layer
    txt = TextClip(
        display_text,
        fontsize=90,
        font=font,
        color="white",
        stroke_color="black",
        stroke_width=3,
        method="caption",
        size=(1500, None),
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
) -> str:
    """Generate a 1920x1080 lyric video and return its path."""
    audio = AudioFileClip(mp3_path)
    duration = audio.duration

    # Use AI-generated image with Ken Burns if available
    if bg_image_path and os.path.exists(bg_image_path):
        bg = _ken_burns_clip(bg_image_path, duration)
    else:
        bg = _get_background_clip(style, duration)

    # Build text clips — each segment gets its own shadow + text
    text_layers = []
    for seg in segments:
        layers = _make_text_clip(seg["text"], seg["start"], seg["end"])
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
    return out_path


# ---------------------------------------------------------------------------
# Step 3 — YouTube Short (30s, vertical)
# ---------------------------------------------------------------------------

def _find_peak_moment(mp3_path: str, window_sec: int = 30) -> float:
    """Find the start time of the most energetic 30-second window."""
    y, sr = librosa.load(mp3_path, sr=22050)
    rms = librosa.feature.rms(y=y, frame_length=2048, hop_length=512)[0]
    frames_per_sec = sr / 512
    window_frames = int(window_sec * frames_per_sec)

    if len(rms) <= window_frames:
        return 0.0

    cumsum = np.cumsum(rms)
    window_sums = cumsum[window_frames:] - cumsum[:-window_frames]
    best_frame = int(np.argmax(window_sums))
    best_time = best_frame / frames_per_sec

    total_duration = len(y) / sr
    if best_time + window_sec > total_duration:
        best_time = max(0, total_duration - window_sec)

    return best_time


def generate_short(
    mp3_path: str,
    video_path: str,
    segments: list[dict],
    job_dir: str,
) -> str:
    """Generate a 1080x1920 vertical short from the most energetic 30s."""
    start_time = _find_peak_moment(mp3_path)
    end_time = start_time + 30

    video = VideoFileClip(video_path).subclip(start_time, end_time)

    scaled = video.resize(width=1080)
    bg = ColorClip(size=(1080, 1920), color=(0, 0, 0)).set_duration(30)
    final = CompositeVideoClip(
        [bg, scaled.set_position(("center", "center"))],
        size=(1080, 1920),
    )
    final = final.set_audio(video.audio)

    out_path = os.path.join(job_dir, "short.mp4")
    final.write_videofile(
        out_path,
        fps=24,
        codec="libx264",
        audio_codec="aac",
        threads=4,
        logger=None,
    )
    video.close()
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
    video_path: str,
    artist: str,
    mp3_path: str,
    job_dir: str,
) -> str:
    """Generate a stylish thumbnail with artist and song name."""
    from PIL import ImageFilter, ImageEnhance

    clip = VideoFileClip(video_path)
    t = min(clip.duration * 0.4, clip.duration - 0.1)
    frame = clip.get_frame(t)
    clip.close()

    img = Image.fromarray(frame)
    img = img.resize((1280, 720), Image.LANCZOS)

    img = img.filter(ImageFilter.GaussianBlur(radius=15))
    enhancer = ImageEnhance.Brightness(img)
    img = enhancer.enhance(0.3)

    overlay = Image.new("RGB", (1280, 720), (60, 30, 120))
    img = Image.blend(img, overlay, alpha=0.3)

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
                font_artist = ImageFont.truetype(fp, 90)
                font_song = ImageFont.truetype(fp, 55)
                break
            except (OSError, IOError):
                continue

    # Artist name centered
    bbox = draw.textbbox((0, 0), artist.upper(), font=font_artist)
    tw = bbox[2] - bbox[0]
    x = (1280 - tw) // 2
    _draw_text_with_outline(draw, (x, 220), artist.upper(), font_artist, fill="white", width=4)

    # Accent line
    draw.rectangle([(440, 340), (840, 344)], fill=(139, 124, 248))

    # Song name centered
    bbox = draw.textbbox((0, 0), song_name, font=font_song)
    tw = bbox[2] - bbox[0]
    x = (1280 - tw) // 2
    _draw_text_with_outline(draw, (x, 400), song_name, font_song, fill=(200, 200, 220), width=3)

    out_path = os.path.join(job_dir, "thumbnail.jpg")
    img.save(out_path, "JPEG", quality=92)
    return out_path
