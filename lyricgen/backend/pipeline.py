"""Full processing pipeline: Whisper → Video → Short → Thumbnail."""

import json
import os
import math
import subprocess
import tempfile

import librosa
import numpy as np
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

    model = whisper.load_model("base")
    result = model.transcribe(mp3_path)
    segments = [
        {"start": seg["start"], "end": seg["end"], "text": seg["text"].strip()}
        for seg in result["segments"]
    ]
    return segments


# ---------------------------------------------------------------------------
# Step 2 — Full HD lyric video
# ---------------------------------------------------------------------------

def _get_background_clip(style: str, duration: float) -> VideoFileClip:
    """Load and loop a background video to match the desired duration."""
    bg_path = os.path.join(BACKGROUNDS_DIR, f"{style}.mp4")
    if not os.path.exists(bg_path):
        bg_path = os.path.join(BACKGROUNDS_DIR, "oscuro.mp4")
    if not os.path.exists(bg_path):
        # Generate a plain black background if no file exists
        from moviepy.editor import ColorClip
        return ColorClip(size=(1920, 1080), color=(13, 13, 20)).set_duration(duration)

    clip = VideoFileClip(bg_path)
    if clip.duration >= duration:
        return clip.subclip(0, duration)
    # Loop the clip
    loops_needed = math.ceil(duration / clip.duration)
    looped = concatenate_videoclips([clip] * loops_needed)
    return looped.subclip(0, duration)


def _make_text_clip(text: str, start: float, end: float) -> TextClip:
    """Create a centered text overlay for one segment."""
    txt = TextClip(
        text,
        fontsize=80,
        color="white",
        stroke_color="black",
        stroke_width=2,
        method="caption",
        size=(1600, None),
        align="center",
    )
    txt = txt.set_position("center").set_start(start).set_end(end)
    return txt


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
    text_clips = [_make_text_clip(s["text"], s["start"], s["end"]) for s in segments]

    video = CompositeVideoClip([bg] + text_clips, size=(1920, 1080))
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

def generate_thumbnail(
    video_path: str,
    artist: str,
    mp3_path: str,
    job_dir: str,
) -> str:
    """Extract a frame at t=5s and overlay artist/song text."""
    # Extract frame using moviepy
    clip = VideoFileClip(video_path)
    t = min(5, clip.duration - 0.1)
    frame = clip.get_frame(t)
    clip.close()

    img = Image.fromarray(frame)
    img = img.resize((1280, 720), Image.LANCZOS)

    draw = ImageDraw.Draw(img)

    # Song name from the mp3 filename
    song_name = os.path.splitext(os.path.basename(mp3_path))[0]

    # Try to load a nice font, fall back to default
    try:
        font_large = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 72)
        font_medium = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 48)
    except (OSError, IOError):
        font_large = ImageFont.load_default()
        font_medium = ImageFont.load_default()

    # Draw artist name (top center)
    bbox = draw.textbbox((0, 0), artist, font=font_large)
    tw = bbox[2] - bbox[0]
    x = (1280 - tw) // 2
    # Text with outline
    for ox, oy in [(-2, -2), (-2, 2), (2, -2), (2, 2)]:
        draw.text((x + ox, 60 + oy), artist, font=font_large, fill="black")
    draw.text((x, 60), artist, font=font_large, fill="white")

    # Draw song name (bottom center)
    bbox = draw.textbbox((0, 0), song_name, font=font_medium)
    tw = bbox[2] - bbox[0]
    x = (1280 - tw) // 2
    y = 720 - 120
    for ox, oy in [(-2, -2), (-2, 2), (2, -2), (2, 2)]:
        draw.text((x + ox, y + oy), song_name, font=font_medium, fill="black")
    draw.text((x, y), song_name, font=font_medium, fill="white")

    out_path = os.path.join(job_dir, "thumbnail.jpg")
    img.save(out_path, "JPEG", quality=90)
    return out_path
