"""End-to-end effect renders through the same entry point used by jobs."""

import os
import shutil
import subprocess

import numpy as np
import pytest
from PIL import Image

import fx_compositor as fx
import pipeline
from render_spec import RenderSpec

pytestmark = pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="ffmpeg/ffprobe not installed",
)


def _photo(path):
    width, height = 640, 360
    x = np.linspace(0, 1, width)
    y = np.linspace(0, 1, height)[:, None]
    image = np.zeros((height, width, 3), dtype=np.uint8)
    image[:, :, 0] = ((x * 210 + y * 30) % 255).astype(np.uint8)
    image[:, :, 1] = ((1 - x) * 170 + y * 60).astype(np.uint8)
    image[:, :, 2] = (40 + np.sin(x * 18)[None, :] * 35 + y * 90).astype(np.uint8)
    Image.fromarray(image).save(path)


def _click_track(path):
    # Alternating strong/soft 80 Hz kicks at 120 BPM. This exercises both the
    # exact timestamps and the low-frequency strength analysis.
    expression = (
        "if(lt(mod(t\\,1)\\,0.055)\\,0.90*sin(2*PI*80*t)\\,"
        "if(lt(mod(t-0.5\\,1)\\,0.055)\\,0.35*sin(2*PI*80*t)\\,0))"
    )
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", f"aevalsrc={expression}:s=44100:d=3",
            "-c:a", "libmp3lame", "-q:a", "3", str(path),
        ],
        check=True,
        capture_output=True,
    )


def _spec():
    return RenderSpec(
        profile="youtube", width=640, height=360, fps=24.0, dar=(16, 9),
        codec="libx264", prores_profile=None, pix_fmt="yuv420p",
        audio_codec="aac", color_primaries="bt709", container="mp4",
    )


@pytest.mark.parametrize(
    "effect",
    [
        "liquid_glass",
        "shadow_play",
        "kaleido",
        "ink_reveal",
        "chromatic_pulse",
        "foto_viva",
        "beat_ripple",
    ],
)
def test_fixed_photo_effect_pipeline_e2e(tmp_path, effect):
    image = tmp_path / "selected-photo.jpg"
    audio = tmp_path / "song.mp3"
    base = tmp_path / "base.mp4"
    job = tmp_path / effect
    job.mkdir()
    _photo(image)
    _click_track(audio)
    pipeline._static_image_to_mp4(
        str(image), str(base), duration=3.0, spec=_spec()
    )

    output = pipeline._render_lyrics_ass(
        str(base), str(audio), [], str(job), 3.0,
        spec=_spec(), font_path="", effect=effect, render_text=False,
    )
    assert os.path.exists(output) and os.path.getsize(output) > 10_000
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=p=0:s=x", output,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert probe.stdout.strip() == "640x360"

    # The delivered MP4, not an intermediate graph, visibly differs from the
    # uploaded photo after the complete preloop/composite/encode/mux chain.
    raw = subprocess.run(
        [
            "ffmpeg", "-loglevel", "error", "-ss", "0.6", "-i", output,
            "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
        ],
        check=True,
        capture_output=True,
    ).stdout
    rendered = np.frombuffer(raw, dtype=np.uint8).reshape(360, 640, 3)
    source = np.asarray(Image.open(image).convert("RGB"), dtype=np.uint8)
    mad = np.abs(rendered.astype(np.int16) - source.astype(np.int16)).mean()
    # Sparse reactive rings intentionally alter less global image area than a
    # geometric warp, but must still survive the final H.264 encode.
    if fx.is_reactive_effect(effect):
        threshold = 0.45
    elif effect in {"shadow_play", "chromatic_pulse"}:
        # Both are intentionally edge-weighted and protect the lyric-safe
        # centre; their global MAD is lower than a full-frame transform.
        threshold = 0.45
    elif effect == "ink_reveal":
        threshold = 0.75
    elif effect == "foto_viva":
        # Localized semantic motion changes less global image area than a
        # full-frame geometric warp, but must survive the complete pipeline.
        threshold = 0.75
    else:
        threshold = 2.0
    assert mad > threshold

    if effect == "beat_ripple":
        rhythm = fx.detect_effect_rhythm(effect, str(audio))
        assert rhythm and 115 <= rhythm.bpm <= 125
        assert len(rhythm.beats) >= 4
        assert max(rhythm.strengths) - min(rhythm.strengths) > 0.1
