"""Foto fija + efectos — the OOM-safe photo background path.

Incident 2026-06-02 ("Rata Blanca"): the photo background rendered a
full-duration Ken Burns pan via moviepy (~7,800 frames animated one-by-one in
Python), which OOM-SIGKILLed the worker on long songs and left the job stuck
in "processing" forever. The fix removes the camera pan: the photo background
is now a STATIC image rendered by ffmpeg (C-level, bounded memory — impossible
to OOM), and the video's life/motion comes from the composable effect overlay
(snow/rain/stars/bokeh/light/aurora) that screen-blends on top.

These tests prove, with REAL ffmpeg, that:
  1. the static render produces a valid full-duration video, and
  2. every effect asset exists and screen-blends onto it without failing.
"""

import os
import shutil
import subprocess

import numpy as np
import pytest
from PIL import Image

import fx_compositor as fx
from pipeline import _static_image_to_mp4

pytestmark = pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="ffmpeg/ffprobe not installed",
)


def _make_image(path, w=1280, h=720):
    # A smooth gradient compresses like a real AI photo (low spatial entropy),
    # unlike random noise which would balloon every keyframe. Representative of
    # what Imagen actually returns.
    xs = np.linspace(0, 255, w, dtype="uint8")
    grad = np.tile(xs, (h, 1))
    rgb = np.stack([grad, np.roll(grad, 137, axis=1), np.flip(grad, axis=1)], axis=2)
    Image.fromarray(rgb.astype("uint8")).save(path)


def _probe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-show_entries", "format=duration",
         "-of", "default=nw=1", str(path)],
        capture_output=True, text=True,
    )
    fields = dict(
        line.split("=", 1) for line in out.stdout.splitlines() if "=" in line
    )
    return int(fields["width"]), int(fields["height"]), float(fields["duration"])


def test_static_image_render_is_valid_and_full_duration(tmp_path):
    """_static_image_to_mp4 renders a still as a full-duration video at the
    target dims — the cheap, OOM-safe replacement for the moviepy Ken Burns."""
    img = tmp_path / "bg.jpg"
    _make_image(img)
    bg = tmp_path / "bg.mp4"
    _static_image_to_mp4(str(img), str(bg), duration=3.0)
    assert bg.exists() and bg.stat().st_size > 0
    w, h, dur = _probe(bg)
    assert (w, h) == (1920, 1080)          # RenderSpec.youtube_default()
    assert 2.5 < dur < 3.7                 # ~3s, encoder rounding tolerance


def test_static_render_handles_long_duration_cheaply(tmp_path):
    """A 5-min song's worth of static background must render fast and small —
    this is the case that OOM-killed the moviepy path."""
    img = tmp_path / "bg.jpg"
    _make_image(img)
    bg = tmp_path / "bg_long.mp4"
    # 150s stands in for the 5-min "Rata Blanca" case — the render mechanism is
    # identical regardless of length, so we keep CI fast. (The moviepy path
    # OOMed because it animated every frame in Python; ffmpeg streams.)
    _static_image_to_mp4(str(img), str(bg), duration=150.0)
    w, h, dur = _probe(bg)
    assert (w, h) == (1920, 1080)
    assert dur > 145
    # An identical-frame stream compresses tiny (proves it isn't materialising
    # thousands of distinct frames the way the moviepy pan did).
    assert bg.stat().st_size < 15 * 1024 * 1024


@pytest.mark.parametrize("effect", list(fx.EFFECTS))
def test_each_effect_asset_exists_and_composites(tmp_path, effect):
    """Every composable effect resolves to a real asset and screen-blends onto
    the static background without ffmpeg erroring — i.e. the effects work."""
    p = fx.effect_path(effect)
    assert p and os.path.exists(p), f"effect asset missing for {effect!r}"

    img = tmp_path / "bg.jpg"
    _make_image(img)
    bg = tmp_path / "bg.mp4"
    _static_image_to_mp4(str(img), str(bg), duration=2.0)

    comp = tmp_path / f"comp_{effect}.mp4"
    # The core of build_video_filter's effect branch: screen-blend the looped
    # fx over the bg (the lyrics subtitles step is exercised separately).
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(bg),
        "-stream_loop", "-1", "-i", p,
        "-filter_complex",
        "[0:v]format=gbrp[b];"
        "[1:v]scale=1920:1080,setpts=PTS-STARTPTS,format=gbrp[f];"
        "[b][f]blend=all_mode=screen:shortest=1,format=yuv420p[o]",
        "-map", "[o]", "-t", "2", "-c:v", "libx264", "-preset", "ultrafast",
        str(comp),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"{effect} composite failed: {r.stderr[-300:]}"
    assert comp.exists() and comp.stat().st_size > 0


def test_build_video_filter_shape():
    """No effect → cheap -vf path; effect → filter_complex with a screen blend
    and the looped fx as an extra input."""
    f_none, complex_none, extra_none = fx.build_video_filter(
        ass_basename="x.ass", font_dir="/tmp", width=1920, height=1080, effect="")
    assert complex_none is False and extra_none == []

    f_snow, complex_snow, extra_snow = fx.build_video_filter(
        ass_basename="x.ass", font_dir="/tmp", width=1920, height=1080, effect="snow")
    assert complex_snow is True
    assert "blend=all_mode=screen" in f_snow
    assert "-stream_loop" in extra_snow
