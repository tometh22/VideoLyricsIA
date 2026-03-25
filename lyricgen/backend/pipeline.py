"""Full processing pipeline: Whisper → Video → Short → Thumbnail."""

import json
import os
import math
import random
import subprocess
import tempfile

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
    CompositeVideoClip,
    TextClip,
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
        update_job(job_id, progress=25)

        # Step 2 — Full lyric video
        update_job(job_id, current_step="video", progress=30)
        video_path = generate_lyric_video(
            mp3_path, segments, style, job_dir, artist
        )
        update_job(job_id, progress=60)

        # Step 3 — YouTube Short
        update_job(job_id, current_step="short", progress=65)
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
        update_job(job_id, status="error", error=str(exc))
        raise


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
# Step 2 — Full HD lyric video
# ---------------------------------------------------------------------------

def _find_background_video(style: str) -> str | None:
    """Find a random background video for the given style.

    Supports two directory layouts:
      1. Folder per style:  backgrounds/oscuro/01.mp4, backgrounds/oscuro/02.mp4 ...
      2. Flat with prefix:  backgrounds/oscuro.mp4, backgrounds/oscuro_2.mp4 ...

    Returns a path or None if nothing is found.
    """
    candidates: list[str] = []

    # Layout 1 — folder per style
    style_dir = os.path.join(BACKGROUNDS_DIR, style)
    if os.path.isdir(style_dir):
        candidates.extend(
            os.path.join(style_dir, f)
            for f in os.listdir(style_dir)
            if f.lower().endswith(".mp4")
        )

    # Layout 2 — flat files: {style}.mp4, {style}_2.mp4, {style}_xxx.mp4
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


def _get_background_clip(style: str, duration: float) -> VideoFileClip:
    """Load and loop a random background video for the given style."""
    # Try the requested style first, then fall back to any available video
    bg_path = _find_background_video(style)
    if bg_path is None:
        bg_path = _find_background_video("oscuro")
    if bg_path is None:
        # Last resort: pick ANY mp4 from backgrounds dir
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
        # Generate a cinematic animated gradient (sunset-like) as fallback
        from moviepy.editor import VideoClip

        # Pre-compute gradient rows for performance
        _rows = np.zeros((1080, 1920, 3), dtype=np.float64)
        for y in range(1080):
            ratio = y / 1080
            # Top: dark teal → Middle: warm pink/magenta → Bottom: dark
            if ratio < 0.4:
                r = 15 + 40 * (ratio / 0.4)
                g = 20 + 30 * (ratio / 0.4)
                b = 50 + 40 * (ratio / 0.4)
            elif ratio < 0.65:
                p = (ratio - 0.4) / 0.25
                r = 55 + 140 * p
                g = 50 - 20 * p
                b = 90 - 30 * p
            else:
                p = (ratio - 0.65) / 0.35
                r = 195 - 170 * p
                g = 30 - 20 * p
                b = 60 - 40 * p
            _rows[y, :] = [r, g, b]

        def _gradient_frame(t):
            """Animated sunset gradient with slow color shift."""
            shift = 15 * np.sin(t * 0.15)
            frame = _rows.copy()
            frame[:, :, 0] = np.clip(frame[:, :, 0] + shift, 0, 255)
            frame[:, :, 2] = np.clip(frame[:, :, 2] - shift * 0.5, 0, 255)
            return frame.astype(np.uint8)

        return VideoClip(_gradient_frame, duration=duration).set_fps(24)

    clip = VideoFileClip(bg_path)
    if clip.duration >= duration:
        return clip.subclip(0, duration)
    # Loop the clip
    loops_needed = math.ceil(duration / clip.duration)
    looped = concatenate_videoclips([clip] * loops_needed)
    return looped.subclip(0, duration)


# Font detection: find a bold italic font, falling back to bold, then any available
_FONT_CANDIDATES = [
    # Bold Italic (preferred — matches reference style)
    "/System/Library/Fonts/Supplemental/Arial Bold Italic.ttf",
    "/System/Library/Fonts/Supplemental/Impact.ttf",
    # Bold fallback
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
_LYRIC_FONT = None
for _fp in _FONT_CANDIDATES:
    if os.path.exists(_fp):
        _LYRIC_FONT = _fp
        break


def generate_lyric_video(
    mp3_path: str,
    segments: list[dict],
    style: str,
    job_dir: str,
    artist: str,
) -> str:
    """Generate a 1920x1080 lyric video and return its path."""
    audio = AudioFileClip(mp3_path)
    duration = audio.duration

    bg = _get_background_clip(style, duration)

    # Build text overlay clips
    text_layers = []
    for s in segments:
        display_text = s["text"].upper()
        start, end = s["start"], s["end"]

        # Shadow (slightly offset for depth)
        shadow = TextClip(
            display_text,
            fontsize=90,
            font=_LYRIC_FONT or "Arial",
            color="black",
            method="caption",
            size=(1500, None),
            align="center",
        ).set_position(lambda t: (213, 543)).set_start(start).set_end(end).set_opacity(0.6)

        # Main text
        txt = TextClip(
            display_text,
            fontsize=90,
            font=_LYRIC_FONT or "Arial",
            color="white",
            stroke_color="black",
            stroke_width=3,
            method="caption",
            size=(1500, None),
            align="center",
        ).set_position("center").set_start(start).set_end(end)

        text_layers.extend([shadow, txt])

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
    # Close clips to free resources
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
    # RMS energy in short frames
    rms = librosa.feature.rms(y=y, frame_length=2048, hop_length=512)[0]
    frames_per_sec = sr / 512
    window_frames = int(window_sec * frames_per_sec)

    if len(rms) <= window_frames:
        return 0.0

    # Sliding window sum
    cumsum = np.cumsum(rms)
    window_sums = cumsum[window_frames:] - cumsum[:-window_frames]
    best_frame = int(np.argmax(window_sums))
    best_time = best_frame / frames_per_sec

    # Make sure we don't exceed audio length
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

    # Resize to fit vertically (1080x1920) with letterboxing
    # Scale width to 1080, then pad height
    scaled = video.resize(width=1080)
    from moviepy.editor import ColorClip, CompositeVideoClip as Comp

    bg = ColorClip(size=(1080, 1920), color=(0, 0, 0)).set_duration(30)
    # Center the scaled clip vertically
    final = Comp(
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

    # Extract a frame from the middle of the video (less likely to be blank)
    clip = VideoFileClip(video_path)
    t = min(clip.duration * 0.4, clip.duration - 0.1)
    frame = clip.get_frame(t)
    clip.close()

    img = Image.fromarray(frame)
    img = img.resize((1280, 720), Image.LANCZOS)

    # Blur and darken the background to hide lyrics and create depth
    img = img.filter(ImageFilter.GaussianBlur(radius=15))
    enhancer = ImageEnhance.Brightness(img)
    img = enhancer.enhance(0.3)

    # Add a purple/brand color overlay
    overlay = Image.new("RGB", (1280, 720), (60, 30, 120))
    img = Image.blend(img, overlay, alpha=0.3)

    draw = ImageDraw.Draw(img)

    # Song name from the mp3 filename
    song_name = os.path.splitext(os.path.basename(mp3_path))[0]

    # Load fonts — try macOS paths, then Linux, then default
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

    # Draw artist name (centered, upper third)
    bbox = draw.textbbox((0, 0), artist.upper(), font=font_artist)
    tw = bbox[2] - bbox[0]
    x = (1280 - tw) // 2
    _draw_text_with_outline(draw, (x, 220), artist.upper(), font_artist, fill="white", width=4)

    # Draw a thin purple accent line
    line_y = 340
    draw.rectangle([(440, line_y), (840, line_y + 4)], fill=(139, 124, 248))

    # Draw song name (centered, lower third)
    bbox = draw.textbbox((0, 0), song_name, font=font_song)
    tw = bbox[2] - bbox[0]
    x = (1280 - tw) // 2
    _draw_text_with_outline(draw, (x, 400), song_name, font_song, fill=(200, 200, 220), width=3)

    out_path = os.path.join(job_dir, "thumbnail.jpg")
    img.save(out_path, "JPEG", quality=92)
    return out_path
